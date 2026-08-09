"""In-memory connection registry for the single-device Relay MVP."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Protocol

from .output_models import ProviderToolResult
from .protocol import (
    AgentError,
    AgentResult,
    Cancel,
    Capabilities,
    InvokeMessage,
    Progress,
    Registered,
)
from .provider_tools import ProviderToolDescriptor
from .providers.base import ProviderToolError, validate_provider_arguments


class JsonSocket(Protocol):
    async def send_json(self, message: object) -> None: ...


class RelayError(Exception):
    """Base error whose messages are safe to return to the control client."""


class AuthenticationError(RelayError):
    pass


class DeviceAlreadyConnectedError(RelayError):
    pass


class DeviceOfflineError(RelayError):
    pass


class UnknownDeviceError(RelayError):
    pass


class DeviceBusyError(RelayError):
    pass


class DuplicateRequestError(RelayError):
    pass


class UnknownRequestError(RelayError):
    pass


class LateResponseError(RelayError):
    """An agent replied after its invocation had already completed."""


class UnsupportedToolError(RelayError):
    pass


class InvalidToolArgumentsError(UnsupportedToolError):
    pass


class RemoteAgentError(RelayError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass
class _Device:
    socket: JsonSocket
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    capabilities: set[str] = field(default_factory=set)
    descriptors: dict[str, ProviderToolDescriptor] = field(default_factory=dict)
    last_heartbeat: float = field(default_factory=time.monotonic)
    progress_request_id: str | None = None
    progress: int | None = None


@dataclass(frozen=True)
class DeviceStatusSnapshot:
    """Safe, immutable public state copied while holding the registry lock."""

    device_id: str | None
    connected: bool
    capabilities: tuple[str, ...]
    invocation_state: str
    progress: int | None
    heartbeat_age_seconds: float | None


class RelayRegistry:
    """Atomically owns at most one authenticated device socket and request."""

    _RECENTLY_COMPLETED_LIMIT = 128

    def __init__(
        self,
        *,
        device_id: str | None = None,
        agent_token: str,
        cancel_send_timeout_seconds: float = 0.25,
    ) -> None:
        self._device_id = device_id
        self._agent_token = agent_token
        self._device: _Device | None = None
        self._pending: dict[str, asyncio.Future[ProviderToolResult]] = {}
        self._recently_completed: dict[str, None] = {}
        self._lock = asyncio.Lock()
        self._cancel_send_timeout_seconds = cancel_send_timeout_seconds

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def current_progress(self) -> int | None:
        """Current bounded progress of the single invocation, if any."""
        return self._device.progress if self._device is not None else None

    @property
    def last_heartbeat(self) -> float | None:
        return self._device.last_heartbeat if self._device is not None else None

    @property
    def announced_capabilities(self) -> frozenset[str]:
        """Return the current Agent announcement for synchronous MCP filtering."""
        device = self._device
        return frozenset(device.capabilities) if device is not None else frozenset()

    @property
    def announced_descriptors(self) -> dict[str, ProviderToolDescriptor]:
        """Return a copy of the selected descriptor announcement."""
        device = self._device
        return {} if device is None else dict(device.descriptors)

    async def status_snapshot(self) -> DeviceStatusSnapshot:
        """Return only safe device state, copied atomically under the lock."""
        async with self._lock:
            device = self._device
            if device is None:
                return DeviceStatusSnapshot(
                    device_id=self._device_id,
                    connected=False,
                    capabilities=(),
                    invocation_state="idle",
                    progress=None,
                    heartbeat_age_seconds=None,
                )
            heartbeat_age = max(0.0, time.monotonic() - device.last_heartbeat)
            return DeviceStatusSnapshot(
                device_id=self._device_id,
                connected=True,
                capabilities=tuple(sorted(device.capabilities)),
                invocation_state="busy" if self._pending else "idle",
                progress=device.progress,
                heartbeat_age_seconds=heartbeat_age,
            )

    async def register(self, socket: JsonSocket, message: object) -> Registered:
        device_id = getattr(message, "device_id", None)
        if not isinstance(device_id, str):
            raise AuthenticationError("invalid device credentials")
        async with self._lock:
            if self._device_id is not None and device_id != self._device_id:
                raise AuthenticationError("invalid device credentials")
            if self._device is not None:
                raise DeviceAlreadyConnectedError("device is already connected")
            if self._device_id is None:
                self._device_id = device_id
            self._device = _Device(socket=socket)
        return Registered(version=1, type="registered", device_id=device_id)

    async def set_capabilities(self, socket: JsonSocket, message: Capabilities) -> None:
        async with self._lock:
            device = self._require_socket(socket)
            device.capabilities = set(message.tools)
            device.descriptors = {
                f"{descriptor.provider_name}.{descriptor.tool_name}": descriptor
                for descriptor in message.descriptors
            }

    async def send(self, socket: JsonSocket, message: object) -> None:
        """Serialize every server-to-agent write for the registered connection."""
        async with self._lock:
            device = self._require_socket(socket)
            write_lock = device.write_lock
        await self._send_serialized(socket, write_lock, message)

    async def heartbeat(self, socket: JsonSocket) -> None:
        async with self._lock:
            self._require_socket(socket).last_heartbeat = time.monotonic()

    async def invoke(
        self,
        device_id: str | None,
        message: InvokeMessage,
        timeout_seconds: float,
    ) -> ProviderToolResult:
        request_id = message.request_id
        async with self._lock:
            if self._device_id is None or (
                device_id is not None and device_id != self._device_id
            ):
                raise UnknownDeviceError("unknown device")
            if self._device is None:
                raise DeviceOfflineError("device is offline")
            if request_id in self._pending:
                raise DuplicateRequestError("duplicate request_id")
            if self._pending:
                raise DeviceBusyError("device already has an invocation in progress")
            self._recently_completed.pop(request_id, None)
            if message.tool_name not in self._device.capabilities:
                raise UnsupportedToolError(f"tool is not declared: {message.tool_name}")
            descriptor = self._device.descriptors.get(message.tool_name)
            if descriptor is not None:
                try:
                    validate_provider_arguments(descriptor, message.arguments)
                except ProviderToolError:
                    raise InvalidToolArgumentsError("invalid tool arguments") from None
            future: asyncio.Future[ProviderToolResult] = (
                asyncio.get_running_loop().create_future()
            )
            self._pending[request_id] = future
            self._device.progress_request_id = request_id
            self._device.progress = None
            socket = self._device.socket
            write_lock = self._device.write_lock
        try:
            try:
                await self._send_serialized(
                    socket, write_lock, message.model_dump(mode="json")
                )
            except asyncio.CancelledError:
                await self._finalize_request(request_id, future)
                await self._send_cancel_if_connected(socket, request_id)
                raise
            except Exception as exc:
                await self._finalize_request(request_id, future)
                self._consume_future(future)
                raise DeviceOfflineError("device is offline") from exc
            try:
                return await asyncio.wait_for(future, timeout_seconds)
            except (TimeoutError, asyncio.CancelledError):
                await self._finalize_request(request_id, future)
                await self._send_cancel_if_connected(socket, request_id)
                raise
        finally:
            await self._finalize_request(request_id, future)

    async def handle_result(self, message: AgentResult) -> None:
        await self._resolve(message.request_id, result=message.result)

    async def handle_error(self, message: AgentError) -> None:
        await self._resolve(
            message.request_id,
            exception=RemoteAgentError(message.error.code, message.error.message),
        )

    async def handle_progress(self, message: Progress) -> None:
        """Record progress only for the request currently in flight."""
        async with self._lock:
            future = self._pending.get(message.request_id)
            if future is None:
                if message.request_id in self._recently_completed:
                    raise LateResponseError("late or duplicate response")
                raise UnknownRequestError("unknown request_id")
            if future.done():
                raise LateResponseError("late or duplicate response")
            if self._device is None:
                raise UnknownRequestError("unknown request_id")
            self._device.progress_request_id = message.request_id
            self._device.progress = message.progress

    async def _resolve(
        self,
        request_id: str,
        *,
        result: ProviderToolResult | None = None,
        exception: Exception | None = None,
    ) -> None:
        async with self._lock:
            future = self._pending.get(request_id)
            if future is None:
                if request_id in self._recently_completed:
                    raise LateResponseError("late or duplicate response")
                raise UnknownRequestError("unknown request_id")
            if future.done():
                raise LateResponseError("late or duplicate response")
            if exception is not None:
                future.set_exception(exception)
            else:
                if result is None:  # pragma: no cover - result/error are exclusive
                    raise RuntimeError("missing result")
                future.set_result(result)

    async def disconnect(self, socket: JsonSocket) -> None:
        async with self._lock:
            if self._device is None or self._device.socket is not socket:
                return
            self._device = None
            for request_id, future in self._pending.items():
                if not future.done():
                    future.set_exception(DeviceOfflineError("device disconnected"))
                self._remember_completed(request_id)
            self._pending.clear()

    async def _send_cancel_if_connected(
        self, socket: JsonSocket, request_id: str
    ) -> None:
        """Best-effort, bounded cancellation after request state is released."""
        async with self._lock:
            if self._device is None or self._device.socket is not socket:
                return
            write_lock = self._device.write_lock
        message = Cancel(
            version=2,
            type="cancel",
            request_id=request_id,
            reason="control request cancelled or timed out",
        ).model_dump(mode="json")
        try:
            await asyncio.wait_for(
                self._send_serialized(socket, write_lock, message),
                timeout=self._cancel_send_timeout_seconds,
            )
        except (Exception, asyncio.CancelledError):
            return

    async def _send_serialized(
        self, socket: JsonSocket, write_lock: asyncio.Lock, message: object
    ) -> None:
        async with write_lock:
            await socket.send_json(message)

    async def _finalize_request(
        self, request_id: str, future: asyncio.Future[dict[str, object]]
    ) -> None:
        """Atomically release request state and leave its bounded tombstone."""
        async with self._lock:
            if self._pending.get(request_id) is future:
                self._pending.pop(request_id)
            self._remember_completed(request_id)
            if (
                self._device is not None
                and self._device.progress_request_id == request_id
            ):
                self._device.progress_request_id = None
                self._device.progress = None

    @staticmethod
    def _consume_future(future: asyncio.Future[dict[str, object]]) -> None:
        """Avoid an unobserved disconnect exception after failed initial I/O."""
        if not future.done():
            future.cancel()
        if not future.cancelled():
            future.exception()

    def _require_socket(self, socket: JsonSocket) -> _Device:
        if self._device is None or self._device.socket is not socket:
            raise AuthenticationError("unregistered socket")
        return self._device

    def _remember_completed(self, request_id: str) -> None:
        """Keep a bounded correlation tombstone for late agent responses."""
        self._recently_completed[request_id] = None
        while len(self._recently_completed) > self._RECENTLY_COMPLETED_LIMIT:
            self._recently_completed.pop(next(iter(self._recently_completed)))

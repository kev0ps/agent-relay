"""Outbound Linux Relay agent and its deliberately small local configuration."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import re
import secrets
import signal
import stat
import sys
import time
from collections.abc import Mapping, Sequence
from ipaddress import ip_address
from pathlib import Path
from typing import Any, AsyncContextManager, Callable, Protocol
from urllib.parse import urlparse

import websockets
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from .capabilities.base import (
    CapabilityName,
    CommandFailedError,
    InvokeMessage,
    LocalCapability,
)
from .capabilities.system import SystemCapability
from .capabilities.terminal import CommandRunnerProtocol, TerminalCapability
from .protocol import (
    MAX_RESULT_JSON_BYTES,
    TOOL_ORDER,
    AgentError,
    AgentResult,
    Cancel,
    Capabilities,
    Heartbeat,
    Registered,
    ToolName,
    parse_server_message,
)
from .runner import CommandRunner


class ConfigurationError(ValueError):
    """A deliberately non-descriptive error for local agent configuration."""

    def __init__(self) -> None:
        super().__init__("invalid agent configuration")


class AgentSettings(BaseModel):
    """Settings controlled only by the local operator, never by INVOKE frames."""

    model_config = ConfigDict(extra="forbid", strict=True)

    server_url: str
    device_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    agent_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$"
    )
    agent_token: SecretStr = Field(repr=False, min_length=1, max_length=256)
    workspace: Path
    allow_insecure_ws: bool = False
    heartbeat_interval_seconds: float = Field(default=15, gt=0, le=3600)
    reconnect_min_seconds: float = Field(default=0.1, gt=0, le=60)
    reconnect_max_seconds: float = Field(default=5, gt=0, le=3600)
    stable_session_seconds: float = Field(default=30, ge=1, le=3600)
    max_ws_message_bytes: int = Field(default=128 * 1024, ge=1024, le=1024 * 1024)
    command_timeout_seconds: float = Field(default=30, gt=0, le=3600)
    stdout_limit: int = Field(default=24 * 1024, ge=0, le=48 * 1024)
    stderr_limit: int = Field(default=24 * 1024, ge=0, le=48 * 1024)
    browser_cdp_url: str | None = None
    browser_allowed_origins: tuple[str, ...] = ()
    browser_connect_timeout_seconds: float = Field(default=5, gt=0, le=30)
    browser_action_timeout_seconds: float = Field(default=10, gt=0, le=30)
    computer_driver_path: Path | None = None
    computer_allowed_app_name: str | None = Field(default=None, min_length=1, max_length=128)
    computer_allowed_window_title: str | None = Field(default=None, min_length=1, max_length=256)
    computer_startup_timeout_seconds: float = Field(default=15, gt=0, le=30)
    computer_action_timeout_seconds: float = Field(default=10, gt=0, le=30)
    computer_shutdown_timeout_seconds: float = Field(default=3, gt=0, le=30)
    computer_max_elements: int = Field(default=300, ge=1, le=1000)
    def __init__(self, /, **data: object) -> None:
        try:
            super().__init__(**data)
        except ValidationError:
            # Pydantic's default rendering includes rejected input values.  Those
            # values can be credentials, so never expose the original error.
            raise ConfigurationError() from None

    @classmethod
    def model_validate(cls, *args: object, **kwargs: object) -> AgentSettings:
        try:
            return super().model_validate(*args, **kwargs)
        except ValidationError:
            raise ConfigurationError() from None

    @classmethod
    def model_validate_json(cls, *args: object, **kwargs: object) -> AgentSettings:
        try:
            return super().model_validate_json(*args, **kwargs)
        except ValidationError:
            raise ConfigurationError() from None

    @classmethod
    def model_validate_strings(cls, *args: object, **kwargs: object) -> AgentSettings:
        try:
            return super().model_validate_strings(*args, **kwargs)
        except ValidationError:
            raise ConfigurationError() from None

    @field_validator("server_url")
    @classmethod
    def valid_server_url(cls, value: str) -> str:
        try:
            parsed = urlparse(value)
        except ValueError as error:
            raise ValueError("invalid server_url") from error
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise ValueError("server_url must be a ws:// or wss:// URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("server_url must not include userinfo")
        if parsed.path != "/ws/agent" or parsed.params or parsed.query or parsed.fragment:
            raise ValueError("server_url must target exactly /ws/agent")
        return value

    @field_validator("workspace")
    @classmethod
    def local_workspace(cls, value: Path) -> Path:
        if not value.is_absolute() or not value.is_dir() or value.is_symlink():
            raise ValueError("workspace must be an absolute existing non-symlink directory")
        return value.resolve(strict=True)

    @model_validator(mode="after")
    def secure_url_and_ranges(self) -> AgentSettings:
        if self.agent_id is None:
            self.agent_id = self.device_id
        elif self.agent_id != self.device_id:
            raise ValueError("device_id and agent_id must match")
        parsed = urlparse(self.server_url)
        if parsed.scheme == "ws" and not _is_explicit_loopback(parsed.hostname or ""):
            if not self.allow_insecure_ws:
                raise ValueError("ws:// is permitted only for explicit loopback hosts")
        if self.reconnect_min_seconds > self.reconnect_max_seconds:
            raise ValueError("reconnect_min_seconds must be <= reconnect_max_seconds")
        result_budget = min(MAX_RESULT_JSON_BYTES, self.max_ws_message_bytes) - 2048
        if self.stdout_limit + self.stderr_limit > result_budget:
            raise ValueError("combined output limits exceed the protocol message budget")
        if bool(self.browser_cdp_url) != bool(self.browser_allowed_origins):
            raise ValueError("partial browser configuration")
        if self.browser_cdp_url:
            _validate_cdp_url(self.browser_cdp_url)
            from .capabilities.browser import normalize_origin
            self.browser_allowed_origins = tuple(
                dict.fromkeys(normalize_origin(origin) for origin in self.browser_allowed_origins)
            )
        computer_values = (
            self.computer_driver_path,
            self.computer_allowed_app_name,
            self.computer_allowed_window_title,
        )
        if any(value is not None for value in computer_values) and not all(
            value is not None for value in computer_values
        ):
            raise ValueError("partial computer configuration")
        if self.computer_driver_path is not None:
            from .capabilities.computer import validate_driver_executable
            self.computer_driver_path = validate_driver_executable(self.computer_driver_path)
        return self

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> AgentSettings:
        try:
            env = os.environ if environ is None else environ
            if any(key in env for key in _CANONICAL_AGENT_KEYS):
                values = _canonical_agent_values(env)
            else:
                values = _legacy_agent_values(env)
            return cls(**values)
        except (ConfigurationError, OSError, ValueError, TypeError):
            raise ConfigurationError() from None




_CANONICAL_AGENT_KEYS = frozenset(
    {"RELAY_URL", "RELAY_AGENT_TOKEN_FILE", "RELAY_AGENT_WORKSPACE"}
)
_AGENT_OPTION_FIELDS = (
    "heartbeat_interval_seconds",
    "reconnect_min_seconds",
    "reconnect_max_seconds",
    "stable_session_seconds",
    "max_ws_message_bytes",
    "command_timeout_seconds",
    "stdout_limit",
    "stderr_limit",
    "browser_connect_timeout_seconds",
    "browser_action_timeout_seconds",
    "computer_startup_timeout_seconds",
    "computer_action_timeout_seconds",
    "computer_shutdown_timeout_seconds",
    "computer_max_elements",
)
_AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _environment_option(env: Mapping[str, str], field: str) -> str | None:
    canonical = "RELAY_AGENT_" + field.upper()
    legacy = "AGENT_RELAY_" + field.upper()
    if canonical in env:
        return env[canonical]
    return env.get(legacy)


def _allow_insecure_ws_from_environment(env: Mapping[str, str]) -> bool:
    raw = env.get("RELAY_ALLOW_INSECURE_WS")
    if raw is None:
        raw = env.get("AGENT_RELAY_ALLOW_INSECURE_WS")
    if raw is None:
        return False
    normalized = raw.strip().lower()
    if normalized not in {"true", "false"}:
        raise ValueError("invalid insecure WebSocket policy")
    return normalized == "true"


def _token_from_environment(
    env: Mapping[str, str], *, token_file_key: str, token_key: str
) -> str:
    token_file = env.get(token_file_key)
    token = _read_token_file(Path(token_file)) if token_file else env.get(token_key)
    if not token:
        raise ConfigurationError()
    return token


def _apply_agent_options(values: dict[str, object], env: Mapping[str, str]) -> None:
    converters: dict[str, Callable[[str], object]] = {
        "heartbeat_interval_seconds": float,
        "reconnect_min_seconds": float,
        "reconnect_max_seconds": float,
        "stable_session_seconds": float,
        "max_ws_message_bytes": int,
        "command_timeout_seconds": float,
        "stdout_limit": int,
        "stderr_limit": int,
        "browser_connect_timeout_seconds": float,
        "browser_action_timeout_seconds": float,
        "computer_startup_timeout_seconds": float,
        "computer_action_timeout_seconds": float,
        "computer_shutdown_timeout_seconds": float,
        "computer_max_elements": int,
    }
    for field in _AGENT_OPTION_FIELDS:
        raw = _environment_option(env, field)
        if raw is not None:
            values[field] = converters[field](raw)

    browser_cdp_url = _environment_option(env, "browser_cdp_url")
    if browser_cdp_url is not None:
        values["browser_cdp_url"] = browser_cdp_url
    browser_origins = _environment_option(env, "browser_allowed_origins")
    if browser_origins is not None:
        values["browser_allowed_origins"] = tuple(
            item.strip() for item in browser_origins.split(",") if item.strip()
        )

    computer_path = _environment_option(env, "computer_driver_path")
    if computer_path is not None:
        values["computer_driver_path"] = Path(computer_path)
    for field in (
        "computer_allowed_app_name",
        "computer_allowed_window_title",
    ):
        raw = _environment_option(env, field)
        if raw is not None:
            values[field] = raw


def _canonical_agent_values(env: Mapping[str, str]) -> dict[str, object]:
    url = env.get("RELAY_URL")
    workspace_value = env.get("RELAY_AGENT_WORKSPACE")
    if not url or not workspace_value:
        raise ConfigurationError()
    token = _token_from_environment(
        env,
        token_file_key="RELAY_AGENT_TOKEN_FILE",
        token_key="RELAY_AGENT_TOKEN",
    )
    workspace = _validated_workspace(Path(workspace_value))
    agent_id = _load_or_create_agent_id(workspace, env.get("RELAY_AGENT_ID"))
    values: dict[str, object] = {
        "server_url": url,
        "device_id": agent_id,
        "agent_id": agent_id,
        "agent_token": token,
        "workspace": workspace,
        "allow_insecure_ws": _allow_insecure_ws_from_environment(env),
    }
    _apply_agent_options(values, env)
    return values


def _legacy_agent_values(env: Mapping[str, str]) -> dict[str, object]:
    server_url = env.get("AGENT_RELAY_SERVER_URL")
    device_id = env.get("AGENT_RELAY_DEVICE_ID")
    workspace_value = env.get("AGENT_RELAY_WORKSPACE")
    if not server_url or not device_id or not workspace_value:
        raise ConfigurationError()
    token = _token_from_environment(
        env,
        token_file_key="AGENT_RELAY_AGENT_TOKEN_FILE",
        token_key="AGENT_RELAY_AGENT_TOKEN",
    )
    values: dict[str, object] = {
        "server_url": server_url,
        "device_id": device_id,
        "agent_id": device_id,
        "agent_token": token,
        "workspace": Path(workspace_value),
        "allow_insecure_ws": _allow_insecure_ws_from_environment(env),
    }
    _apply_agent_options(values, env)
    return values


def _validated_workspace(path: Path) -> Path:
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise ValueError("workspace must be an absolute existing non-symlink directory")
    return path.resolve(strict=True)


def _validate_agent_id(value: str) -> str:
    if not _AGENT_ID_PATTERN.fullmatch(value):
        raise ValueError("invalid agent identity")
    return value


def _private_local_path(path: Path, *, directory: bool) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise ValueError("agent identity path must not be a symlink")
    if directory:
        valid_type = stat.S_ISDIR(info.st_mode)
        expected_mode = 0o700
    else:
        valid_type = stat.S_ISREG(info.st_mode)
        expected_mode = 0o600
    if (
        not valid_type
        or stat.S_IMODE(info.st_mode) != expected_mode
        or (hasattr(os, "getuid") and info.st_uid != os.getuid())
    ):
        raise ValueError("agent identity path is not private")
    return info


def _ensure_agent_state_dir(workspace: Path) -> Path:
    state_dir = workspace / ".agent-relay"
    created = False
    try:
        state_dir.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    if created:
        try:
            os.chmod(state_dir, 0o700)
        except OSError:
            state_dir.rmdir()
            raise
    _private_local_path(state_dir, directory=True)
    return state_dir


def _read_agent_id_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
            or info.st_size > 128
        ):
            raise ValueError("agent identity file is not private")
        value = os.read(fd, 129).decode("utf-8").strip()
    finally:
        os.close(fd)
    return _validate_agent_id(value)


def _create_agent_id_file(path: Path, value: str) -> str:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        existing = _read_agent_id_file(path)
        if existing != value:
            raise ValueError("existing agent identity differs")
        return existing
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        encoded = value.encode("utf-8")
        written = 0
        while written < len(encoded):
            count = os.write(fd, encoded[written:])
            if count <= 0:
                raise OSError("could not persist agent identity")
            written += count
        os.fsync(fd)
    except OSError:
        path.unlink(missing_ok=True)
        raise
    finally:
        os.close(fd)
    return value


def _load_or_create_agent_id(workspace: Path, configured: str | None) -> str:
    selected = _validate_agent_id(configured) if configured else None
    state_dir = _ensure_agent_state_dir(workspace)
    identity_path = state_dir / "agent-id"
    try:
        _private_local_path(identity_path, directory=False)
    except FileNotFoundError:
        if selected is None:
            selected = "agent-" + secrets.token_hex(16)
        return _create_agent_id_file(identity_path, selected)
    existing = _read_agent_id_file(identity_path)
    if selected is not None and selected != existing:
        raise ValueError("existing agent identity differs")
    return existing


def _is_explicit_loopback(hostname: str) -> bool:
    if hostname == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validate_cdp_url(value: str) -> None:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid browser endpoint") from exc
    if parsed.scheme != "http" or not parsed.hostname or port is None or port == 0:
        raise ValueError("invalid browser endpoint")
    if not _is_explicit_loopback(parsed.hostname) or parsed.hostname in {"0.0.0.0", "::"}:
        raise ValueError("invalid browser endpoint")
    if parsed.username is not None or parsed.password is not None or parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("invalid browser endpoint")


def _read_token_file(path: Path) -> str:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
            or info.st_size > 4096
        ):
            raise ValueError("agent token file is not a private regular file")
        chunks: list[bytes] = []
        remaining = 4097
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining == 0:
            raise ValueError("agent token file is too large")
        token = b"".join(chunks).decode("utf-8").strip()
    finally:
        os.close(fd)
    if not token:
        raise ValueError("agent token file is empty")
    return token


class TextSocket(Protocol):
    async def send(self, payload: str) -> None: ...
    async def recv(self) -> str: ...


class RelayAgent:
    """One outbound connection; a received cancel can never yield a late result."""

    def __init__(
        self,
        settings: AgentSettings,
        *,
        runner: CommandRunnerProtocol | None = None,
        capabilities: Sequence[LocalCapability] | None = None,
        connector: Callable[..., AsyncContextManager[TextSocket]] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.settings = settings
        configured_runner = runner or CommandRunner(settings.workspace, timeout_seconds=settings.command_timeout_seconds, stdout_limit=settings.stdout_limit, stderr_limit=settings.stderr_limit)
        configured_capabilities = (
            [
                SystemCapability(),
                TerminalCapability(configured_runner),
            ]
            if capabilities is None
            else list(capabilities)
        )
        # Bind Computer to the allowlisted desktop before Browser creates an
        # additional Chromium context/window that is also visible to AT-SPI.
        if capabilities is None and settings.computer_driver_path:
            from .capabilities.computer import ComputerCapability
            assert settings.computer_allowed_app_name is not None
            assert settings.computer_allowed_window_title is not None
            configured_capabilities.append(ComputerCapability(
                settings.computer_driver_path,
                settings.computer_allowed_app_name,
                settings.computer_allowed_window_title,
                startup_timeout_seconds=settings.computer_startup_timeout_seconds,
                action_timeout_seconds=settings.computer_action_timeout_seconds,
                shutdown_timeout_seconds=settings.computer_shutdown_timeout_seconds,
                max_elements=settings.computer_max_elements,
            ))
        if capabilities is None and settings.browser_cdp_url:
            from .capabilities.browser import BrowserCapability
            configured_capabilities.append(BrowserCapability(settings.browser_cdp_url, settings.browser_allowed_origins, connect_timeout_seconds=settings.browser_connect_timeout_seconds, action_timeout_seconds=settings.browser_action_timeout_seconds))
        self._capabilities = self._index_capabilities(configured_capabilities)
        self._unique_capabilities = tuple(dict.fromkeys(map(id, configured_capabilities)))
        self._capability_objects = {id(item): item for item in configured_capabilities}
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False
        self._stop_event = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self._connector = connector or websockets.connect
        self._session_registered = False
        self._registered_at: float | None = None
        self._monotonic = monotonic or time.monotonic

    def stop(self) -> None:
        self._stop_event.set()

    def _connection_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "max_size": self.settings.max_ws_message_bytes,
            "proxy": None,
        }
        headers = {
            "Authorization": "Bearer " + self.settings.agent_token.get_secret_value()
        }
        try:
            parameters = inspect.signature(self._connector).parameters
        except (TypeError, ValueError):
            parameter_names: set[str] = set()
        else:
            parameter_names = set(parameters)
        header_option = (
            "additional_headers"
            if "additional_headers" in parameter_names
            or "extra_headers" not in parameter_names
            else "extra_headers"
        )
        options[header_option] = headers
        return options

    async def run(self) -> None:
        try:
            delay = self.settings.reconnect_min_seconds
            while not self._stop_event.is_set():
                self._session_registered = False
                self._registered_at = None
                try:
                    await self._start_capabilities()
                    async with self._connector(
                        self.settings.server_url,
                        **self._connection_options(),
                    ) as socket:
                        await self.run_session(socket)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    if os.environ.get("AGENT_RELAY_NATIVE_DEBUG") == "1":
                        phase = getattr(error, "startup_phase", None)
                        detail = (
                            f" phase-{phase}"
                            if isinstance(phase, str)
                            else ""
                        )
                        print(
                            f"agent reconnect: {type(error).__name__}{detail}",
                            file=sys.stderr,
                            flush=True,
                        )
                    pass
                if self._session_was_stable():
                    delay = self.settings.reconnect_min_seconds
                if not self._stop_event.is_set():
                    await self._sleep_or_stop(delay)
                    delay = min(delay * 2, self.settings.reconnect_max_seconds)
        finally:
            await self.aclose()

    @staticmethod
    def _index_capabilities(
        capabilities: Sequence[LocalCapability],
    ) -> dict[CapabilityName, LocalCapability]:
        indexed: dict[ToolName, LocalCapability] = {}
        for capability in capabilities:
            if not capability.tools:
                raise ValueError("unsupported local capability")
            for tool in capability.tools:
                if tool not in TOOL_ORDER:
                    raise ValueError("unsupported local capability")
                if tool in indexed:
                    raise ValueError(f"duplicate local capability: {tool}")
                indexed[tool] = capability
        if not indexed:
            raise ValueError("at least one local capability is required")
        return indexed

    async def _start_capabilities(self) -> None:
        for ident in self._unique_capabilities:
            await self._capability_objects[ident].start()

    async def aclose(self) -> None:
        """Close every configured capability exactly once."""
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._aclose_owned())
        cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                await asyncio.shield(self._close_task)
                break
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
                if self._close_task.done():
                    break
        if cancellation is not None:
            raise cancellation

    async def _aclose_owned(self) -> None:
        for ident in self._unique_capabilities:
            capability = self._capability_objects[ident]
            await asyncio.gather(capability.aclose(), return_exceptions=True)

    def _session_was_stable(self) -> bool:
        """Only reset after a registered connection outlives the local threshold."""
        return (
            self._registered_at is not None
            and self._monotonic() - self._registered_at >= self.settings.stable_session_seconds
        )

    async def _sleep_or_stop(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except TimeoutError:
            pass

    async def run_session(self, socket: TextSocket) -> None:
        await self._send(
            socket,
            {
                "version": 1,
                "type": "register",
                "device_id": self.settings.agent_id,
            },
        )
        registered = await self._receive(socket)
        if not isinstance(registered, Registered) or registered.device_id != self.settings.agent_id:
            raise ValueError("server did not confirm registration")
        self._session_registered = True
        self._registered_at = self._monotonic()
        await self._send(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                tools=[tool for tool in TOOL_ORDER if tool in self._capabilities],
            ).model_dump(mode="json"),
        )
        heartbeat = asyncio.create_task(self._heartbeat(socket))
        action: asyncio.Task[None] | None = None
        action_request_id: str | None = None
        receive: asyncio.Task[object] | None = asyncio.create_task(self._receive(socket))
        stopping = asyncio.create_task(self._stop_event.wait())
        unavailable = {asyncio.create_task(self._capability_objects[ident].wait_unavailable()) for ident in self._unique_capabilities}
        try:
            while not self._stop_event.is_set():
                wait_for = {receive, stopping, *unavailable}
                if action is not None:
                    wait_for.add(action)
                done, _ = await asyncio.wait(wait_for, return_when=asyncio.FIRST_COMPLETED)
                if stopping in done:
                    break
                if done & unavailable:
                    if action is not None:
                        action.cancel()
                        await asyncio.gather(action, return_exceptions=True)
                        action = None
                    raise ConnectionError("local capability unavailable")
                if action is not None and action in done:
                    await action
                    action = None
                    action_request_id = None
                if receive in done:
                    message = receive.result()
                    receive = asyncio.create_task(self._receive(socket))
                    if isinstance(message, InvokeMessage.__args__):
                        if action is not None:
                            await self._send_error(socket, message.request_id, "busy", "an action is already running")
                        else:
                            action = asyncio.create_task(self._perform(socket, message))
                            action_request_id = message.request_id
                    elif isinstance(message, Cancel):
                        if action is not None and action_request_id == message.request_id:
                            action.cancel()
                            await asyncio.gather(action, return_exceptions=True)
                            action = None
                            action_request_id = None
                    else:
                        raise ValueError("unexpected server message")
        finally:
            heartbeat.cancel()
            if receive is not None:
                receive.cancel()
            stopping.cancel()
            for task in unavailable:
                task.cancel()
            if action is not None:
                action.cancel()
            await asyncio.gather(heartbeat, stopping, *unavailable, *(item for item in (receive, action) if item is not None), return_exceptions=True)

    async def _heartbeat(self, socket: TextSocket) -> None:
        while not self._stop_event.is_set():
            await self._sleep_or_stop(self.settings.heartbeat_interval_seconds)
            if not self._stop_event.is_set():
                await self._send(socket, Heartbeat(version=1, type="heartbeat").model_dump(mode="json"))

    async def _perform(self, socket: TextSocket, message: InvokeMessage) -> None:
        try:
            capability = self._capabilities.get(message.tool)
            if capability is None:
                raise ValueError("unsupported local capability")
            result = await capability.invoke(message)
            await self._send(socket, AgentResult(version=1, type="result", request_id=message.request_id, result=result).model_dump(mode="json"))
        except asyncio.CancelledError:
            raise
        except CommandFailedError:
            await self._send_error(
                socket,
                message.request_id,
                "command_failed",
                "configured command failed",
            )
        except Exception:
            await self._send_error(socket, message.request_id, "agent_error", "local action failed")

    async def _receive(self, socket: TextSocket) -> object:
        text = await socket.recv()
        if not isinstance(text, str) or len(text.encode("utf-8")) > self.settings.max_ws_message_bytes:
            raise ValueError("invalid server frame")
        return parse_server_message(json.loads(text))

    async def _send(self, socket: TextSocket, message: object) -> None:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > self.settings.max_ws_message_bytes:
            raise ValueError("outbound message exceeds limit")
        async with self._write_lock:
            await socket.send(payload)

    async def _send_error(self, socket: TextSocket, request_id: str, code: str, message: str) -> None:
        await self._send(socket, AgentError(version=1, type="error", request_id=request_id, error={"code": code, "message": message}).model_dump(mode="json"))

async def _run_with_signal_handlers(agent: RelayAgent) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, agent.stop)
        except NotImplementedError:
            # Windows event loops do not expose add_signal_handler(). The
            # default console handling still interrupts the process safely.
            continue
    await agent.run()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Agent Relay outbound agent")
    parser.add_argument("--agent-token", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.agent_token is not None:
        parser.error(
            "--agent-token is unsafe; use RELAY_AGENT_TOKEN_FILE or "
            "AGENT_RELAY_AGENT_TOKEN_FILE"
        )
    try:
        settings = AgentSettings.from_environment()
    except ConfigurationError:
        parser.error("invalid agent configuration")
    asyncio.run(_run_with_signal_handlers(RelayAgent(settings)))


def config_main(
    argv: Sequence[str] | None = None,
    *,
    prog: str = "agent-relay client config",
) -> int:
    """Validate, display, or initialize client configuration safely."""
    parser = argparse.ArgumentParser(prog=prog)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("validate", help="validate the current environment")
    actions.add_parser("show", help="show effective configuration with secrets masked")
    init_parser = actions.add_parser(
        "init", help="create a non-secret configuration template"
    )
    init_parser.add_argument(
        "--output",
        default=".env.client.example",
        help="new template path (refuses to overwrite an existing file)",
    )
    args = parser.parse_args(argv)

    if args.action == "init":
        output = Path(args.output)
        try:
            _write_client_config_template(output)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"created client configuration template: {output}")
        return 0

    try:
        settings = AgentSettings.from_environment()
    except ConfigurationError:
        parser.error("invalid client configuration")

    if args.action == "validate":
        print("client configuration is valid")
        return 0

    payload = settings.model_dump(mode="json")
    payload["agent_token"] = "[REDACTED]"
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _write_client_config_template(path: Path) -> None:
    template = """# Agent Relay client configuration template.
# Export these values before running: agent-relay client run
RELAY_URL=wss://relay.example.invalid/ws/agent
RELAY_AGENT_WORKSPACE=/absolute/path/to/workspace
RELAY_AGENT_TOKEN_FILE=/absolute/path/to/agent.token
# RELAY_AGENT_ID=optional-provisioned-id
"""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise ValueError(f"refusing to overwrite existing file: {path}") from None
    except OSError as exc:
        raise ValueError(f"could not create configuration template: {path}") from exc
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(template)
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise ValueError(f"could not create configuration template: {path}") from exc


if __name__ == "__main__":
    main()

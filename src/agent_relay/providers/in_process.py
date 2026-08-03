"""Trusted adapter for locally owned in-process provider implementations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence

from ..json_bounds import JsonValue
from ..output_models import ProviderToolResult
from ..provider_tools import ProviderToolDescriptor
from .base import (
    DEFAULT_PROVIDER_CLOSE_TIMEOUT_SECONDS,
    DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    ProviderCleanupError,
    ProviderToolError,
    UnknownProviderToolError,
    _ProviderDeadlineExceeded,
    bounded_arguments,
    bounded_descriptors,
    bounded_result,
    drain_pending_tasks,
    run_bounded,
    validate_provider_arguments,
    validate_timeout,
)

ProviderHandler = Callable[
    [Mapping[str, JsonValue]], Awaitable[ProviderToolResult | Mapping[str, object]]
]
CloseHandler = Callable[[], Awaitable[None]]


class InProcessProviderToolClient:
    """Bind closed descriptors to callables supplied by trusted local setup."""

    def __init__(
        self,
        descriptors: Sequence[ProviderToolDescriptor],
        handlers: Mapping[str, ProviderHandler],
        *,
        close_handler: CloseHandler | None = None,
        timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        close_timeout_seconds: float = DEFAULT_PROVIDER_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        self._descriptors = bounded_descriptors(descriptors)
        names = {descriptor.tool_name for descriptor in self._descriptors}
        if set(handlers) != names:
            raise ValueError("local handlers must exactly match provider descriptors")
        self._handlers = dict(handlers)
        self._close_handler = close_handler
        self._timeout_seconds = validate_timeout(
            timeout_seconds, label="timeout_seconds"
        )
        self._close_timeout_seconds = validate_timeout(
            close_timeout_seconds, label="close_timeout_seconds"
        )
        self._closed = False
        self._available = True
        self._unavailable = asyncio.Event()
        self._close_lock = asyncio.Lock()
        self._pending_tasks: set[asyncio.Task[object]] = set()

    async def list_tools(self) -> Sequence[ProviderToolDescriptor]:
        self._require_available()
        return self._descriptors

    async def wait_unavailable(self) -> None:
        await self._unavailable.wait()

    async def call_tool(
        self, tool_name: str, arguments: Mapping[str, JsonValue]
    ) -> ProviderToolResult:
        self._require_available()
        descriptor = next(
            (tool for tool in self._descriptors if tool.tool_name == tool_name),
            None,
        )
        handler = self._handlers.get(tool_name)
        if descriptor is None or handler is None:
            raise UnknownProviderToolError("unknown provider tool")
        bounded_arguments(arguments)
        validate_provider_arguments(descriptor, arguments)
        try:
            raw_result = await run_bounded(
                lambda: handler(arguments),
                self._timeout_seconds,
                self._pending_tasks,
            )
        except _ProviderDeadlineExceeded:
            self._mark_unavailable()
            raise
        except asyncio.CancelledError:
            self._mark_unavailable()
            raise
        except Exception:
            call_failed = True
        else:
            call_failed = False
        if call_failed:
            raise ProviderToolError("provider tool call failed") from None
        return bounded_result(raw_result)

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._mark_unavailable()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._close_timeout_seconds
            try:
                drained = await drain_pending_tasks(
                    self._pending_tasks, deadline - loop.time()
                )
            except asyncio.CancelledError:
                raise
            if not drained:
                raise ProviderCleanupError("provider cleanup failed")
            if self._close_handler is None:
                self._closed = True
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ProviderCleanupError("provider cleanup failed")
            try:
                await run_bounded(
                    self._close_handler, remaining, self._pending_tasks
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                cleanup_failed = True
            else:
                cleanup_failed = False
            if cleanup_failed:
                raise ProviderCleanupError("provider cleanup failed") from None
            self._closed = True

    def _require_available(self) -> None:
        if not self._available:
            raise ProviderToolError("provider client unavailable")

    def _mark_unavailable(self) -> None:
        self._available = False
        self._unavailable.set()


__all__ = ["InProcessProviderToolClient"]

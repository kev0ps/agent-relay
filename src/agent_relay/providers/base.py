"""Minimal, bounded provider tool client boundary."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Protocol

from pydantic import ValidationError

from ..json_bounds import JsonBoundsError, JsonValue, validate_json_bounds
from ..output_models import ProviderToolResult
from ..provider_tools import ProviderToolCatalog, ProviderToolDescriptor

DEFAULT_PROVIDER_TIMEOUT_SECONDS = 30.0
DEFAULT_PROVIDER_CLOSE_TIMEOUT_SECONDS = 3.0


class ProviderToolError(RuntimeError):
    """A provider operation failed without exposing provider details."""


class ProviderConnectionError(ProviderToolError):
    """The locally configured provider transport was unavailable."""


class ProviderTimeoutError(ProviderToolError):
    """A provider operation exceeded its configured deadline."""


class _ProviderDeadlineExceeded(ProviderTimeoutError):
    """Internal marker emitted only by the adapter's bounded runner."""


class ProviderCleanupError(ProviderToolError):
    """Provider cleanup failed without exposing provider details."""


class UnknownProviderToolError(ProviderToolError):
    """A call named a tool outside the provider's bounded inventory."""


class ProviderToolClient(Protocol):
    async def list_tools(self) -> Sequence[ProviderToolDescriptor]: ...

    async def call_tool(
        self, tool_name: str, arguments: Mapping[str, JsonValue]
    ) -> ProviderToolResult: ...

    async def close(self) -> None: ...


def validate_timeout(value: float, *, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{label} must be greater than zero")
    return float(value)


def bounded_descriptors(
    values: Sequence[ProviderToolDescriptor | Mapping[str, object]],
) -> tuple[ProviderToolDescriptor, ...]:
    try:
        catalog = ProviderToolCatalog.model_validate({"tools": list(values)})
    except (ValidationError, JsonBoundsError, TypeError, ValueError):
        invalid = True
    else:
        return tuple(catalog.tools)
    if invalid:
        raise ProviderToolError("invalid provider tool inventory")


def bounded_arguments(arguments: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    try:
        validate_json_bounds(arguments, require_object=True, label="provider arguments")
    except (JsonBoundsError, TypeError, ValueError):
        invalid = True
    else:
        return arguments
    if invalid:
        raise ProviderToolError("invalid provider arguments")


def bounded_result(value: object) -> ProviderToolResult:
    try:
        return ProviderToolResult.model_validate(_model_data(value))
    except (ValidationError, JsonBoundsError, TypeError, ValueError):
        invalid = True
    if invalid:
        raise ProviderToolError("invalid provider result")


async def run_bounded(
    operation: Callable[[], Awaitable[object]],
    timeout_seconds: float,
    pending_tasks: set[asyncio.Task[object]],
) -> object:
    task = asyncio.create_task(operation())
    pending_tasks.add(task)
    task.add_done_callback(
        lambda completed: _release_task(completed, pending_tasks)
    )
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
    except asyncio.CancelledError:
        task.cancel()
        raise
    if task in done:
        return task.result()

    # A provider may suppress cancellation. Detach it so the public operation
    # still returns at its deadline. The owning client keeps it registered until
    # it actually finishes because Python cannot force-kill an in-process task.
    task.cancel()
    raise _ProviderDeadlineExceeded("provider operation timed out")


async def drain_pending_tasks(
    pending_tasks: set[asyncio.Task[object]], timeout_seconds: float
) -> bool:
    tasks = set(pending_tasks)
    if not tasks:
        return True
    for task in tasks:
        task.cancel()
    try:
        done, pending = await asyncio.wait(tasks, timeout=max(0.0, timeout_seconds))
    except asyncio.CancelledError:
        for task in pending_tasks:
            task.cancel()
        raise
    for task in done:
        _release_task(task, pending_tasks)
    return not pending


def _release_task(
    task: asyncio.Task[object], pending_tasks: set[asyncio.Task[object]]
) -> None:
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass
    pending_tasks.discard(task)


def _model_data(value: object) -> object:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json", by_alias=True, exclude_none=True)
    return value


__all__ = [
    "DEFAULT_PROVIDER_CLOSE_TIMEOUT_SECONDS",
    "DEFAULT_PROVIDER_TIMEOUT_SECONDS",
    "ProviderConnectionError",
    "ProviderCleanupError",
    "ProviderToolClient",
    "ProviderToolError",
    "ProviderTimeoutError",
    "UnknownProviderToolError",
]

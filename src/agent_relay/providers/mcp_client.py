"""Adapter for a locally configured MCP provider transport."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ..diagnostics import debug as _debug_log
from ..json_bounds import (
    MAX_JSON_COLLECTION_ITEMS,
    JsonBoundsError,
    JsonValue,
    validate_json_schema,
)
from ..output_models import ProviderToolResult
from ..provider_tools import (
    MAX_PROVIDER_DESCRIPTION_LENGTH,
    MAX_PROVIDER_TOOLS,
    ProviderRiskClass,
    ProviderToolDescriptor,
)
from .base import (
    DEFAULT_PROVIDER_CLOSE_TIMEOUT_SECONDS,
    DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    ProviderCleanupError,
    ProviderConnectionError,
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


class McpTransport(Protocol):
    async def list_tools(self, cursor: str | None = None) -> object: ...

    async def call_tool(
        self, name: str, arguments: Mapping[str, JsonValue]
    ) -> object: ...

    async def close(self) -> None: ...


def _debug_cua_inventory_failure(provider_name: str, category: str) -> None:
    if provider_name == "cua":
        _debug_log(f"cua provider inventory failure: category={category}")


def _debug_cua_descriptor_failure(provider_name: str, category: str) -> None:
    if provider_name == "cua":
        _debug_log(f"cua provider descriptor failure: category={category}")


def _normalize_cua_schema_bounds(value: object) -> object:
    """Copy a CUA schema while filling the shared finite collection bounds."""
    if isinstance(value, list):
        return [_normalize_cua_schema_bounds(item) for item in value]
    if not isinstance(value, dict):
        return value
    normalized = {
        key: _normalize_cua_schema_bounds(child) for key, child in value.items()
    }
    items = normalized.get("items")
    if items is not None and items is not False and "maxItems" not in normalized:
        normalized["maxItems"] = MAX_JSON_COLLECTION_ITEMS
    additional = normalized.get("additionalProperties")
    if isinstance(additional, dict) and "maxProperties" not in normalized:
        normalized["maxProperties"] = MAX_JSON_COLLECTION_ITEMS
    return normalized


def _schema_failure_category(value: object) -> str | None:
    """Map schema-bound failures to a closed diagnostic category."""
    try:
        validate_json_schema(value)
    except JsonBoundsError as error:
        detail = str(error)
        if "maxItems" in detail:
            return "array-max-items"
        if "additionalProperties" in detail or "maxProperties" in detail:
            return "object-properties"
        if "items" in detail:
            return "array-items"
        if "recursive" in detail or "$ref" in detail:
            return "reference"
        if "unsupported" in detail:
            return "unsupported-keyword"
        if "unbounded" in detail:
            return "unbounded"
        return "schema-bounds"
    except (TypeError, ValueError):
        return "schema-bounds"
    return None


class NativeMcpSession(Protocol):
    """The native operations used inside the owner actor's session context."""

    async def list_tools(self, cursor: str | None = None) -> object: ...

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class _NativeMcpCommand:
    operation: Literal["list_tools", "call_tool", "close"]
    result: asyncio.Future[object]
    cursor: str | None = None
    name: str | None = None
    arguments: dict[str, Any] | None = None


class NativeMcpSessionTransport:
    """Serialize one locally configured MCP context in a private owner task.

    Python cannot force-kill an uncooperative coroutine. Public facade calls
    remain bounded, become unavailable during close, and never run cleanup
    concurrently with a session operation, while the tracked owner finishes the
    single context exit if and when the provider cooperates.
    """

    def __init__(
        self,
        session_context_factory: Callable[
            [], AbstractAsyncContextManager[NativeMcpSession]
        ],
    ) -> None:
        if not callable(session_context_factory):
            raise TypeError("session_context_factory must be callable")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError(
                "native MCP session transport requires a running event loop"
            ) from None
        self._session_context_factory = session_context_factory
        self._commands: asyncio.Queue[_NativeMcpCommand] = asyncio.Queue()
        self._close_result: asyncio.Future[object] | None = None
        self._owner = loop.create_task(self._run_owner())
        self._owner.add_done_callback(_consume_task_result)

    async def list_tools(self, cursor: str | None = None) -> object:
        return await self._request(
            _NativeMcpCommand(
                "list_tools", asyncio.get_running_loop().create_future(), cursor=cursor
            )
        )

    async def call_tool(
        self, name: str, arguments: Mapping[str, JsonValue]
    ) -> object:
        return await self._request(
            _NativeMcpCommand(
                "call_tool",
                asyncio.get_running_loop().create_future(),
                name=name,
                arguments=dict(arguments),
            )
        )

    async def close(self) -> None:
        if self._close_result is None:
            self._close_result = asyncio.get_running_loop().create_future()
            self._commands.put_nowait(
                _NativeMcpCommand("close", self._close_result)
            )
        await asyncio.shield(self._close_result)

    async def _request(self, command: _NativeMcpCommand) -> object:
        if self._close_result is not None:
            raise ProviderToolError("provider client unavailable")
        self._commands.put_nowait(command)
        try:
            return await command.result
        except asyncio.CancelledError:
            command.result.cancel()
            raise

    async def _run_owner(self) -> None:
        close_result: asyncio.Future[object] | None = None
        enter_failed = False
        try:
            context = self._session_context_factory()
            async with context as session:
                close_result = await self._serve(session)
        except BaseException:
            enter_failed = True
        else:
            if close_result is not None and not close_result.done():
                close_result.set_result(None)
            return

        if close_result is not None and not close_result.done():
            close_result.set_exception(
                ProviderCleanupError("provider cleanup failed")
            )
            return
        if enter_failed:
            await self._reject_after_enter_failure()

    async def _serve(
        self, session: NativeMcpSession
    ) -> asyncio.Future[object]:
        while True:
            command = await self._commands.get()
            if command.operation == "close":
                return command.result
            if command.result.cancelled():
                continue
            failed = False
            try:
                if command.operation == "list_tools":
                    value = await session.list_tools(cursor=command.cursor)
                else:
                    assert command.name is not None
                    value = await session.call_tool(
                        command.name, command.arguments
                    )
            except BaseException:
                failed = True
            if command.result.done():
                continue
            if failed:
                if command.operation == "list_tools":
                    error: ProviderToolError = ProviderConnectionError(
                        "provider connection failed"
                    )
                else:
                    error = ProviderToolError("provider tool call failed")
                command.result.set_exception(error)
            else:
                command.result.set_result(value)

    async def _reject_after_enter_failure(self) -> None:
        while True:
            command = await self._commands.get()
            if command.result.cancelled():
                continue
            if command.operation == "close":
                command.result.set_exception(
                    ProviderCleanupError("provider cleanup failed")
                )
                return
            command.result.set_exception(
                ProviderConnectionError("provider connection failed")
            )


class McpProviderToolClient:
    """Expose one preconfigured local MCP connection as a bounded provider."""

    def __init__(
        self,
        transport: McpTransport,
        *,
        provider_name: str,
        risk: ProviderRiskClass = "blocked",
        timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        close_timeout_seconds: float = DEFAULT_PROVIDER_CLOSE_TIMEOUT_SECONDS,
        allowed_tool_names: Collection[str] | None = None,
    ) -> None:
        self._transport = transport
        self._provider_name = provider_name
        if allowed_tool_names is not None and any(
            not isinstance(name, str) or not name for name in allowed_tool_names
        ):
            raise ValueError("allowed_tool_names must contain non-empty strings")
        self._allowed_tool_names = (
            None if allowed_tool_names is None else frozenset(allowed_tool_names)
        )
        self._risk: ProviderRiskClass = risk
        self._timeout_seconds = validate_timeout(
            timeout_seconds, label="timeout_seconds"
        )
        self._close_timeout_seconds = validate_timeout(
            close_timeout_seconds, label="close_timeout_seconds"
        )
        self._tools: tuple[ProviderToolDescriptor, ...] | None = None
        self._closed = False
        self._available = True
        self._unavailable = asyncio.Event()
        self._close_lock = asyncio.Lock()
        self._list_lock = asyncio.Lock()
        self._pending_tasks: set[asyncio.Task[object]] = set()

    async def list_tools(self) -> Sequence[ProviderToolDescriptor]:
        deadline = asyncio.get_running_loop().time() + self._timeout_seconds
        try:
            return await self._list_tools(deadline)
        except _ProviderDeadlineExceeded:
            self._mark_unavailable()
            raise
        except asyncio.CancelledError:
            self._mark_unavailable()
            raise
        except ProviderConnectionError:
            self._mark_unavailable()
            raise

    async def wait_unavailable(self) -> None:
        await self._unavailable.wait()

    async def _list_tools(
        self, deadline: float
    ) -> Sequence[ProviderToolDescriptor]:
        self._require_available()
        if self._tools is not None:
            return self._tools
        async with self._list_lock:
            self._require_available()
            if self._tools is not None:
                return self._tools
            try:
                descriptors: list[ProviderToolDescriptor] = []
                cursor: str | None = None
                seen_cursors: set[str] = set()
                loop = asyncio.get_running_loop()
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise _ProviderDeadlineExceeded(
                            "provider operation timed out"
                        )
                    response = await _list_page(
                        self._transport,
                        cursor,
                        remaining,
                        self._pending_tasks,
                    )
                    failure_category = "page-shape"
                    try:
                        raw_tools = _field(response, "tools")
                        if not isinstance(raw_tools, (list, tuple)):
                            failure_category = "tools-field"
                            raise ValueError
                        for tool in raw_tools:
                            if self._allowed_tool_names is not None:
                                # Unselected CUA tools may carry blocked or executable
                                # schemas; never validate or expose those descriptors.
                                raw_name = _field(tool, "name")
                                if (
                                    not isinstance(raw_name, str)
                                    or raw_name not in self._allowed_tool_names
                                ):
                                    continue
                            try:
                                descriptor = _descriptor(
                                    self._provider_name, self._risk, tool
                                )
                            except Exception:
                                failure_category = "descriptor"
                                raise
                            descriptors.append(descriptor)
                        if len(descriptors) > MAX_PROVIDER_TOOLS:
                            failure_category = "too-many-tools"
                            raise ValueError
                        next_cursor = _field(
                            response, "nextCursor", "next_cursor", default=None
                        )
                        if next_cursor is not None and (
                            not isinstance(next_cursor, str)
                            or not next_cursor
                            or len(next_cursor) > 2048
                            or next_cursor in seen_cursors
                            or len(seen_cursors) >= MAX_PROVIDER_TOOLS
                        ):
                            failure_category = "cursor"
                            raise ValueError
                    except (Exception,):
                        _debug_cua_inventory_failure(
                            self._provider_name, failure_category
                        )
                        invalid_inventory = True
                    else:
                        invalid_inventory = False
                    if invalid_inventory:
                        raise ProviderToolError("invalid provider tool inventory")
                    if next_cursor is None:
                        break
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
                self._tools = bounded_descriptors(descriptors)
            except (ProviderToolError, asyncio.CancelledError):
                raise
            return self._tools

    async def call_tool(
        self, tool_name: str, arguments: Mapping[str, JsonValue]
    ) -> ProviderToolResult:
        self._require_available()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_seconds
        try:
            tools = await self._list_tools(deadline)
        except _ProviderDeadlineExceeded:
            self._mark_unavailable()
            raise
        except asyncio.CancelledError:
            self._mark_unavailable()
            raise
        except ProviderConnectionError:
            self._mark_unavailable()
            raise
        descriptor = next(
            (tool for tool in tools if tool.tool_name == tool_name),
            None,
        )
        if descriptor is None:
            raise UnknownProviderToolError("unknown provider tool")
        bounded_arguments(arguments)
        validate_provider_arguments(descriptor, arguments)
        remaining = deadline - loop.time()
        if remaining <= 0:
            self._mark_unavailable()
            raise _ProviderDeadlineExceeded("provider operation timed out")
        try:
            raw_result = await run_bounded(
                lambda: self._transport.call_tool(tool_name, arguments),
                remaining,
                self._pending_tasks,
            )
        except _ProviderDeadlineExceeded:
            self._mark_unavailable()
            raise
        except asyncio.CancelledError:
            self._mark_unavailable()
            raise
        except (ConnectionError, OSError):
            connection_failed = True
            call_failed = False
        except Exception:
            connection_failed = False
            call_failed = True
        else:
            connection_failed = call_failed = False
        if connection_failed:
            self._mark_unavailable()
            raise ProviderConnectionError("provider connection failed") from None
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
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ProviderCleanupError("provider cleanup failed")
            try:
                await run_bounded(
                    self._transport.close, remaining, self._pending_tasks
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


def _descriptor(
    provider_name: str, risk: ProviderRiskClass, tool: object
) -> ProviderToolDescriptor:
    name = _field(tool, "name")
    description = _field(tool, "description", default=None)
    if description is None or description == "":
        description = "Provider tool"
    elif isinstance(description, str):
        description = description[:MAX_PROVIDER_DESCRIPTION_LENGTH]
    input_schema = _field(tool, "inputSchema", "input_schema")
    output_schema = _field(tool, "outputSchema", "output_schema", default=None)
    if provider_name == "cua":
        # cua-driver 0.8.3 omits some collection bounds; fill them with the
        # existing Relay limit without changing generic-provider validation.
        input_schema = _normalize_cua_schema_bounds(input_schema)
        if output_schema is not None:
            output_schema = _normalize_cua_schema_bounds(output_schema)
    input_schema_failure = _schema_failure_category(input_schema)
    output_schema_failure = (
        _schema_failure_category(output_schema) if output_schema is not None else None
    )
    schema_failure = None
    if input_schema_failure is not None:
        schema_failure = f"input-schema-{input_schema_failure}"
    elif output_schema_failure is not None:
        schema_failure = f"output-schema-{output_schema_failure}"
    annotations = _field(tool, "annotations", default={})
    if annotations is None:
        annotations = {}
    try:
        return ProviderToolDescriptor.model_validate(
            {
                "provider_name": provider_name,
                "tool_name": name,
                "public_name": name,
                "description": description,
                "input_schema": input_schema,
                "output_schema": output_schema,
                "annotations": _model_data(annotations),
                "risk": risk,
            }
        )
    except Exception as error:
        category = schema_failure or "model"
        error_details = getattr(error, "errors", None)
        if callable(error_details):
            try:
                details = error_details()
                if isinstance(details, list):
                    for detail in details:
                        if not isinstance(detail, Mapping):
                            continue
                        location = detail.get("loc", ())
                        if location:
                            field = str(location[0])
                            if field in {
                                "inputSchema",
                                "input_schema",
                                "outputSchema",
                                "output_schema",
                                "annotations",
                                "description",
                                "name",
                                "tool_name",
                            } and schema_failure is None:
                                category = field.replace("_", "-")
                                break
            except Exception:
                pass
        _debug_cua_descriptor_failure(provider_name, category)
        raise


async def _list_page(
    transport: McpTransport,
    cursor: str | None,
    timeout_seconds: float,
    pending_tasks: set[asyncio.Task[object]],
) -> object:
    try:
        response = await run_bounded(
            lambda: transport.list_tools(cursor), timeout_seconds, pending_tasks
        )
    except _ProviderDeadlineExceeded:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        connection_failed = True
    else:
        return response
    if connection_failed:
        raise ProviderConnectionError("provider connection failed") from None


_MISSING = object()


def _field(value: object, *names: str, default: object = _MISSING) -> object:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
    else:
        for name in names:
            if hasattr(value, name):
                return getattr(value, name)
    if default is not _MISSING:
        return default
    raise ValueError("provider response is missing a required field")


def _model_data(value: object) -> object:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json", by_alias=True, exclude_none=True)
    return value


def _consume_task_result(task: asyncio.Task[None]) -> None:
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


__all__ = [
    "McpProviderToolClient",
    "McpTransport",
    "NativeMcpSession",
    "NativeMcpSessionTransport",
]

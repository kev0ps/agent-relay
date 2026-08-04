from __future__ import annotations

import asyncio
import traceback
from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest
from mcp import ClientSession

from agent_relay.json_bounds import MAX_JSON_BYTES, JsonValue
from agent_relay.output_models import ProviderToolResult
from agent_relay.provider_tools import (
    MAX_PROVIDER_DESCRIPTION_LENGTH,
    ProviderToolDescriptor,
)
from agent_relay.providers.base import (
    ProviderCleanupError,
    ProviderConnectionError,
    ProviderToolClient,
    ProviderToolError,
    UnknownProviderToolError,
    validate_provider_arguments,
)
from agent_relay.providers.in_process import InProcessProviderToolClient
from agent_relay.providers.mcp_client import (
    McpProviderToolClient,
    NativeMcpSessionTransport,
)

_OPAQUE_SCHEMA = {
    "anyOf": [
        {"type": "string", "maxLength": 256},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "null"},
        {
            "type": "array",
            "items": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "maxItems": 8,
        },
    ]
}
_NESTED_ITEM_SCHEMA = {
    "anyOf": [
        {"type": "integer"},
        {"type": "boolean"},
        {"type": "null"},
        {
            "type": "object",
            "properties": {"native": {"type": "string", "maxLength": 64}},
            "required": ["native"],
            "additionalProperties": False,
            "maxProperties": 1,
        },
    ]
}


def descriptor(name: str = "snapshot") -> ProviderToolDescriptor:
    return ProviderToolDescriptor(
        provider_name="browser",
        tool_name=name,
        public_name=f"browser:{name}",
        description="A locally owned test tool",
        input_schema={
            "type": "object",
            "properties": {
                "nested": {
                    "type": "array",
                    "items": _NESTED_ITEM_SCHEMA,
                    "maxItems": 8,
                },
                "opaque": _OPAQUE_SCHEMA,
                "value": _OPAQUE_SCHEMA,
            },
            "additionalProperties": False,
        },
        risk="read_only",
    )


def result(text: str = "ok") -> ProviderToolResult:
    return ProviderToolResult(content=[{"type": "text", "text": text}])


def assert_sanitized(error: BaseException, message: str) -> None:
    formatted = "".join(traceback.format_exception(error))
    assert str(error) == message
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "password" not in formatted
    assert "very-secret" not in formatted


def test_provider_tool_client_protocol_has_minimal_surface() -> None:
    methods = {
        name
        for name, value in vars(ProviderToolClient).items()
        if callable(value) and not name.startswith("_")
    }
    assert methods == {
        "list_tools",
        "call_tool",
        "close",
    }


def test_descriptors_cannot_serialize_execution_configuration() -> None:
    payload = descriptor().model_dump(mode="json", by_alias=True)
    for field in ("handler", "module", "executable", "method", "endpoint"):
        with pytest.raises(ValueError):
            ProviderToolDescriptor.model_validate(payload | {field: "secret"})


def test_provider_argument_schema_is_checked_before_call() -> None:
    tool = ProviderToolDescriptor(
        provider_name="browser",
        tool_name="fill",
        public_name="relay_browser_fill",
        description="fill a field",
        input_schema={
            "type": "object",
            "properties": {
                "locator": {
                    "type": "object",
                    "properties": {"role": {"type": "string", "minLength": 1}},
                    "required": ["role"],
                    "additionalProperties": False,
                },
                "value": {"type": "string", "maxLength": 8},
            },
            "required": ["locator", "value"],
            "additionalProperties": False,
        },
        risk="interaction",
    )

    valid_arguments: dict[str, JsonValue] = {
        "locator": {"role": "textbox"},
        "value": "hello",
    }
    assert validate_provider_arguments(tool, valid_arguments) == valid_arguments
    invalid_arguments: tuple[dict[str, JsonValue], ...] = (
        {},
        {"locator": {"role": "textbox"}},
        {"locator": {"role": "textbox"}, "value": "too long!"},
        {"locator": {"role": "textbox"}, "value": "ok", "extra": True},
        {"locator": {"role": "textbox"}, "value": 1},
    )
    for invalid in invalid_arguments:
        with pytest.raises(ProviderToolError, match="do not match tool schema"):
            validate_provider_arguments(tool, invalid)


def test_in_process_passes_json_arguments_without_semantic_conversion() -> None:
    async def scenario() -> None:
        received: Mapping[str, JsonValue] | None = None

        async def handler(arguments: Mapping[str, JsonValue]) -> ProviderToolResult:
            nonlocal received
            received = arguments
            return result()

        arguments: dict[str, JsonValue] = {
            "nested": [1, True, None, {"native": "value"}]
        }
        client = InProcessProviderToolClient([descriptor()], {"snapshot": handler})
        assert await client.call_tool("snapshot", arguments) == result()
        assert received is arguments

    asyncio.run(scenario())


def test_unknown_in_process_tool_is_rejected_before_handler_execution() -> None:
    async def scenario() -> None:
        calls = 0

        async def handler(arguments: Mapping[str, JsonValue]) -> ProviderToolResult:
            nonlocal calls
            calls += 1
            return result()

        client = InProcessProviderToolClient([descriptor()], {"snapshot": handler})
        with pytest.raises(UnknownProviderToolError, match="unknown provider tool"):
            await client.call_tool("missing", {})
        assert calls == 0

    asyncio.run(scenario())


def test_in_process_result_and_arguments_are_bounded() -> None:
    async def scenario() -> None:
        async def handler(arguments: Mapping[str, JsonValue]) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": "x" * (MAX_JSON_BYTES + 1)}]}

        client = InProcessProviderToolClient([descriptor()], {"snapshot": handler})
        with pytest.raises(ProviderToolError) as invalid_result:
            await client.call_tool("snapshot", {})
        assert_sanitized(invalid_result.value, "invalid provider result")
        with pytest.raises(ProviderToolError) as invalid_arguments:
            await client.call_tool("snapshot", {"value": object()})  # type: ignore[dict-item]
        assert_sanitized(invalid_arguments.value, "invalid provider arguments")

    asyncio.run(scenario())


def test_timeout_is_normalized_and_cancellation_is_preserved() -> None:
    async def scenario() -> None:
        blocker = asyncio.Event()

        async def handler(arguments: Mapping[str, JsonValue]) -> ProviderToolResult:
            await blocker.wait()
            return result()

        client = InProcessProviderToolClient(
            [descriptor()], {"snapshot": handler}, timeout_seconds=0.01
        )
        with pytest.raises(ProviderToolError) as timed_out:
            await client.call_tool("snapshot", {})
        assert_sanitized(timed_out.value, "provider operation timed out")
        await asyncio.wait_for(client.wait_unavailable(), 1)
        with pytest.raises(ProviderToolError, match="provider client unavailable"):
            await client.call_tool("snapshot", {})

        cancellation_client = InProcessProviderToolClient(
            [descriptor()], {"snapshot": handler}, timeout_seconds=1
        )
        task = asyncio.create_task(cancellation_client.call_tool("snapshot", {}))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProviderToolError, match="provider client unavailable"):
            await cancellation_client.call_tool("snapshot", {})

    asyncio.run(scenario())


def test_in_process_timeout_cannot_be_suppressed_into_success() -> None:
    async def scenario() -> None:
        cancelled = asyncio.Event()
        release = asyncio.Event()

        async def handler(arguments: Mapping[str, JsonValue]) -> ProviderToolResult:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                return result("late secret password very-secret")

        client = InProcessProviderToolClient(
            [descriptor()], {"snapshot": handler}, timeout_seconds=0.01
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(ProviderToolError) as caught:
            await client.call_tool("snapshot", {})
        assert loop.time() - started < 0.05
        assert_sanitized(caught.value, "provider operation timed out")
        await cancelled.wait()
        release.set()
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_mcp_inventory_timeout_cannot_be_suppressed_into_success() -> None:
    class UncooperativeTransport(FakeMcpTransport):
        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return await super().list_tools(cursor)

    async def scenario() -> None:
        client = McpProviderToolClient(
            UncooperativeTransport(),
            provider_name="cua-driver",
            timeout_seconds=0.01,
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(ProviderToolError) as caught:
            await client.list_tools()
        assert loop.time() - started < 0.05
        assert_sanitized(caught.value, "provider operation timed out")
        await asyncio.sleep(0)

    asyncio.run(scenario())


class FakeMcpTransport:
    def __init__(self) -> None:
        self.call_count = 0
        self.close_count = 0

    async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
        assert cursor is None
        return {
            "tools": [
                {
                    "name": "capture",
                    "description": "Capture the synthetic desktop",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"opaque": _OPAQUE_SCHEMA},
                        "additionalProperties": False,
                    },
                }
            ]
        }

    async def call_tool(
        self, name: str, arguments: Mapping[str, JsonValue]
    ) -> dict[str, object]:
        self.call_count += 1
        return {
            "content": [{"type": "text", "text": str(arguments["opaque"])}],
            "structuredContent": {"native": arguments["opaque"]},
            "isError": False,
        }

    async def close(self) -> None:
        self.close_count += 1


def test_mcp_adapter_maps_inventory_and_passes_native_result() -> None:
    async def scenario() -> None:
        transport = FakeMcpTransport()
        client = McpProviderToolClient(
            transport, provider_name="cua-driver", risk="interaction"
        )
        tools = await client.list_tools()
        assert [(tool.provider_name, tool.tool_name) for tool in tools] == [
            ("cua-driver", "capture")
        ]
        output = await client.call_tool("capture", {"opaque": [1, None]})
        assert output.structured_content == {"native": [1, None]}
        assert output.content[0].type == "text"

    asyncio.run(scenario())


def test_mcp_adapter_filters_unselected_provider_schemas_before_validation() -> None:
    class ProviderWithBlockedUnselectedTool(FakeMcpTransport):
        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            return {
                "tools": [
                    {
                        "name": "capture",
                        "description": "Capture the synthetic desktop",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"opaque": _OPAQUE_SCHEMA},
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "execute_javascript",
                        "description": "Blocked unselected tool",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "javascript": {"type": "string", "maxLength": 256}
                            },
                            "additionalProperties": False,
                        },
                    },
                ]
            }

    async def scenario() -> None:
        client = McpProviderToolClient(
            ProviderWithBlockedUnselectedTool(),
            provider_name="cua-driver",
            risk="interaction",
            allowed_tool_names={"capture"},
        )
        assert [tool.tool_name for tool in await client.list_tools()] == ["capture"]

    asyncio.run(scenario())


def test_native_mcp_session_transport_actor_forwards_and_exits_once() -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.cursors: list[str | None] = []
            self.calls: list[tuple[str, dict[str, Any]]] = []
            self.exits: list[tuple[object, object, object]] = []

        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            self.cursors.append(cursor)
            return {"tools": []}

        async def call_tool(
            self, name: str, arguments: dict[str, Any] | None = None
        ) -> dict[str, object]:
            self.calls.append((name, arguments or {}))
            return {"content": []}

    async def scenario() -> None:
        session = FakeSession()
        entries = 0

        @asynccontextmanager
        async def session_context():
            nonlocal entries
            entries += 1
            try:
                yield session
            finally:
                session.exits.append((None, None, None))

        transport = NativeMcpSessionTransport(session_context)
        assert await transport.list_tools("page-2") == {"tools": []}
        assert await transport.call_tool("capture", {"opaque": [1]}) == {
            "content": []
        }
        await transport.close()
        await transport.close()
        assert entries == 1
        assert session.cursors == ["page-2"]
        assert session.calls == [("capture", {"opaque": [1]})]
        assert session.exits == [(None, None, None)]

    asyncio.run(scenario())


def test_native_mcp_actor_owns_real_client_session_context_lifecycle() -> None:
    async def scenario() -> None:
        incoming_send, incoming_receive = anyio.create_memory_object_stream(1)
        outgoing_send, outgoing_receive = anyio.create_memory_object_stream(1)

        @asynccontextmanager
        async def session_context():
            async with ClientSession(incoming_receive, outgoing_send) as session:
                yield session
            await incoming_send.aclose()
            await incoming_receive.aclose()
            await outgoing_send.aclose()
            await outgoing_receive.aclose()

        client = McpProviderToolClient(
            NativeMcpSessionTransport(session_context),
            provider_name="cua-driver",
        )
        await client.close()
        await client.close()

    asyncio.run(scenario())


def test_native_mcp_actor_skips_cancelled_commands_and_ignores_late_results() -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.cursors: list[str | None] = []
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            self.cursors.append(cursor)
            if cursor == "running":
                self.started.set()
                await self.release.wait()
            return {"tools": [], "cursor": cursor}

    async def scenario() -> None:
        session = FakeSession()

        @asynccontextmanager
        async def session_context():
            yield session

        transport = NativeMcpSessionTransport(session_context)
        running = asyncio.create_task(transport.list_tools("running"))
        await session.started.wait()
        queued = asyncio.create_task(transport.list_tools("queued"))
        await asyncio.sleep(0)
        running.cancel()
        queued.cancel()
        for task in (running, queued):
            with pytest.raises(asyncio.CancelledError):
                await task

        session.release.set()
        assert await transport.list_tools("next") == {
            "tools": [],
            "cursor": "next",
        }
        assert session.cursors == ["running", "next"]
        await transport.close()

    asyncio.run(scenario())


def test_native_mcp_session_transport_requires_context_factory() -> None:
    with pytest.raises(TypeError):
        NativeMcpSessionTransport(object())  # type: ignore[arg-type,call-arg]


def test_native_mcp_session_transport_requires_running_owner_task() -> None:
    with pytest.raises(RuntimeError):
        NativeMcpSessionTransport(lambda: object())  # type: ignore[arg-type,return-value]


def test_native_mcp_close_timeout_shares_one_uncooperative_context_exit() -> None:
    class FakeSession:
        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            return {"tools": []}

    async def scenario() -> None:
        session = FakeSession()
        exit_started = asyncio.Event()
        release_exit = asyncio.Event()
        exit_count = 0

        @asynccontextmanager
        async def session_context():
            nonlocal exit_count
            try:
                yield session
            finally:
                exit_count += 1
                exit_started.set()
                while not release_exit.is_set():
                    try:
                        await release_exit.wait()
                    except asyncio.CancelledError:
                        pass

        client = McpProviderToolClient(
            NativeMcpSessionTransport(session_context),
            provider_name="cua-driver",
            close_timeout_seconds=0.01,
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(ProviderCleanupError) as first:
            await client.close()
        assert loop.time() - started < 0.05
        assert_sanitized(first.value, "provider cleanup failed")
        await exit_started.wait()

        with pytest.raises(ProviderCleanupError) as second:
            await client.close()
        assert_sanitized(second.value, "provider cleanup failed")
        assert exit_count == 1

        release_exit.set()
        await asyncio.sleep(0)
        await client.close()
        await client.close()
        assert exit_count == 1

    asyncio.run(scenario())


def test_mcp_inventory_follows_pagination_and_caches_complete_result() -> None:
    class PaginatedTransport(FakeMcpTransport):
        def __init__(self) -> None:
            super().__init__()
            self.cursors: list[str | None] = []

        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            self.cursors.append(cursor)
            if cursor is None:
                return {
                    "tools": [self.tool("capture")],
                    "nextCursor": "page-2",
                }
            return {"tools": [self.tool("click")]}

        @staticmethod
        def tool(name: str) -> dict[str, object]:
            return {
                "name": name,
                "inputSchema": {"type": "object", "additionalProperties": False},
            }

    async def scenario() -> None:
        transport = PaginatedTransport()
        client = McpProviderToolClient(transport, provider_name="cua-driver")
        assert [tool.tool_name for tool in await client.list_tools()] == [
            "capture",
            "click",
        ]
        assert [tool.tool_name for tool in await client.list_tools()] == [
            "capture",
            "click",
        ]
        assert transport.cursors == [None, "page-2"]

    asyncio.run(scenario())


def test_mcp_inventory_uses_one_deadline_across_all_pages() -> None:
    class SlowPaginatedTransport(FakeMcpTransport):
        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            await asyncio.sleep(0.035)
            return {"tools": [], "nextCursor": "page-2"} if cursor is None else {"tools": []}

    async def scenario() -> None:
        client = McpProviderToolClient(
            SlowPaginatedTransport(),
            provider_name="cua-driver",
            timeout_seconds=0.05,
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(ProviderToolError) as caught:
            await client.list_tools()
        elapsed = loop.time() - started
        assert_sanitized(caught.value, "provider operation timed out")
        assert elapsed < 0.07
        with pytest.raises(ProviderToolError, match="provider client unavailable"):
            await client.list_tools()

    asyncio.run(scenario())


def test_mcp_call_uses_one_deadline_for_inventory_and_invocation() -> None:
    class SlowTransport(FakeMcpTransport):
        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            await asyncio.sleep(0.035)
            return await super().list_tools(cursor)

        async def call_tool(
            self, name: str, arguments: Mapping[str, JsonValue]
        ) -> dict[str, object]:
            await asyncio.sleep(0.035)
            return await super().call_tool(name, arguments)

    async def scenario() -> None:
        client = McpProviderToolClient(
            SlowTransport(), provider_name="cua-driver", timeout_seconds=0.05
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(ProviderToolError) as caught:
            await client.call_tool("capture", {"opaque": "value"})
        assert loop.time() - started < 0.07
        assert_sanitized(caught.value, "provider operation timed out")

    asyncio.run(scenario())


@pytest.mark.parametrize("bad_cursor", ["repeat", "", 7])
def test_mcp_inventory_rejects_bad_or_repeated_cursor_without_looping(
    bad_cursor: object,
) -> None:
    class BadCursorTransport(FakeMcpTransport):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            self.calls += 1
            next_cursor = cursor if bad_cursor == "repeat" and cursor else bad_cursor
            if bad_cursor == "repeat" and cursor is None:
                next_cursor = "same"
            return {"tools": [], "nextCursor": next_cursor}

    async def scenario() -> None:
        transport = BadCursorTransport()
        client = McpProviderToolClient(transport, provider_name="cua-driver")
        with pytest.raises(ProviderToolError, match="invalid provider tool inventory"):
            await client.list_tools()
        assert transport.calls <= 2

    asyncio.run(scenario())


def test_mcp_unknown_tool_is_rejected_before_tools_call() -> None:
    async def scenario() -> None:
        transport = FakeMcpTransport()
        client = McpProviderToolClient(transport, provider_name="cua-driver")
        with pytest.raises(UnknownProviderToolError):
            await client.call_tool("missing", {})
        assert transport.call_count == 0

    asyncio.run(scenario())


def test_connection_errors_are_sanitized() -> None:
    class BrokenTransport(FakeMcpTransport):
        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            raise OSError("wss://user:password@host/?token=very-secret")

    async def scenario() -> None:
        client = McpProviderToolClient(BrokenTransport(), provider_name="cua-driver")
        with pytest.raises(ProviderConnectionError) as caught:
            await client.list_tools()
        assert_sanitized(caught.value, "provider connection failed")

    asyncio.run(scenario())


def test_call_connection_errors_are_sanitized() -> None:
    class BrokenTransport(FakeMcpTransport):
        async def call_tool(
            self, name: str, arguments: Mapping[str, JsonValue]
        ) -> dict[str, object]:
            raise OSError("wss://user:password@host/?token=very-secret")

    async def scenario() -> None:
        client = McpProviderToolClient(BrokenTransport(), provider_name="cua-driver")
        with pytest.raises(ProviderConnectionError) as caught:
            await client.call_tool("capture", {})
        assert_sanitized(caught.value, "provider connection failed")

    asyncio.run(scenario())


def test_in_process_provider_tool_error_is_sanitized() -> None:
    async def scenario() -> None:
        async def handler(arguments: Mapping[str, JsonValue]) -> ProviderToolResult:
            raise ProviderToolError(
                "wss://user:password@host/?token=very-secret"
            )

        client = InProcessProviderToolClient(
            [descriptor()], {"snapshot": handler}
        )
        with pytest.raises(ProviderToolError) as caught:
            await client.call_tool("snapshot", {})
        assert_sanitized(caught.value, "provider tool call failed")

    asyncio.run(scenario())


def test_mcp_list_provider_tool_error_is_sanitized() -> None:
    class BrokenTransport(FakeMcpTransport):
        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            raise ProviderToolError(
                "wss://user:password@host/?token=very-secret"
            )

    async def scenario() -> None:
        client = McpProviderToolClient(
            BrokenTransport(), provider_name="cua-driver"
        )
        with pytest.raises(ProviderConnectionError) as caught:
            await client.list_tools()
        assert_sanitized(caught.value, "provider connection failed")

    asyncio.run(scenario())


def test_mcp_call_provider_tool_error_is_sanitized() -> None:
    class BrokenTransport(FakeMcpTransport):
        async def call_tool(
            self, name: str, arguments: Mapping[str, JsonValue]
        ) -> dict[str, object]:
            raise ProviderToolError(
                "wss://user:password@host/?token=very-secret"
            )

    async def scenario() -> None:
        client = McpProviderToolClient(
            BrokenTransport(), provider_name="cua-driver"
        )
        with pytest.raises(ProviderToolError) as caught:
            await client.call_tool("capture", {})
        assert type(caught.value) is ProviderToolError
        assert_sanitized(caught.value, "provider tool call failed")

    asyncio.run(scenario())


def test_malformed_mcp_descriptor_is_inventory_error_without_sensitive_context() -> None:
    class MalformedTransport(FakeMcpTransport):
        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            return {
                "tools": [
                    {
                        "name": "capture",
                        "inputSchema": "wss://user:password@host/?token=very-secret",
                    }
                ]
            }

    async def scenario() -> None:
        client = McpProviderToolClient(MalformedTransport(), provider_name="cua-driver")
        with pytest.raises(ProviderToolError) as caught:
            await client.list_tools()
        assert type(caught.value) is ProviderToolError
        assert_sanitized(caught.value, "invalid provider tool inventory")

    asyncio.run(scenario())


def test_provider_description_is_bounded_before_descriptor_validation() -> None:
    class LongDescriptionTransport(FakeMcpTransport):
        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            return {
                "tools": [
                    {
                        "name": "capture",
                        "description": "x" * (MAX_PROVIDER_DESCRIPTION_LENGTH + 100),
                        "inputSchema": {"type": "object", "additionalProperties": False},
                    }
                ]
            }

    async def scenario() -> None:
        client = McpProviderToolClient(
            LongDescriptionTransport(), provider_name="cua"
        )
        tools = await client.list_tools()
        assert len(tools) == 1
        assert len(tools[0].description) == MAX_PROVIDER_DESCRIPTION_LENGTH

    asyncio.run(scenario())


def test_successful_close_is_idempotent() -> None:
    async def scenario() -> None:
        transport = FakeMcpTransport()
        client = McpProviderToolClient(transport, provider_name="cua-driver")
        await client.close()
        await client.close()
        assert transport.close_count == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("client_kind", ["in_process", "mcp"])
def test_successful_close_rejects_later_operations(client_kind: str) -> None:
    async def scenario() -> None:
        if client_kind == "in_process":
            client = InProcessProviderToolClient(
                [descriptor()], {"snapshot": lambda arguments: _async_result()}
            )
            tool_name = "snapshot"
        else:
            client = McpProviderToolClient(
                FakeMcpTransport(), provider_name="cua-driver"
            )
            tool_name = "capture"
        await client.close()
        for operation in (client.list_tools(), client.call_tool(tool_name, {})):
            with pytest.raises(ProviderToolError) as caught:
                await operation
            assert_sanitized(caught.value, "provider client unavailable")

    async def _async_result() -> ProviderToolResult:
        return result()

    asyncio.run(scenario())


@pytest.mark.parametrize("client_kind", ["in_process", "mcp"])
@pytest.mark.parametrize("first_outcome", ["cancel", "failure", "timeout"])
def test_close_can_retry_after_cancelled_or_failed_cleanup(
    client_kind: str, first_outcome: str
) -> None:
    async def scenario() -> None:
        attempts = 0
        started = asyncio.Event()

        async def cleanup() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1 and first_outcome == "cancel":
                started.set()
                await asyncio.Event().wait()
            if attempts == 1 and first_outcome == "timeout":
                await asyncio.Event().wait()
            if attempts == 1:
                raise RuntimeError("synthetic cleanup failure")

        if client_kind == "in_process":
            client = InProcessProviderToolClient(
                [descriptor()], {"snapshot": lambda arguments: _async_result()},
                close_handler=cleanup,
                close_timeout_seconds=0.01,
            )
        else:
            transport = FakeMcpTransport()
            transport.close = cleanup  # type: ignore[method-assign]
            client = McpProviderToolClient(
                transport,
                provider_name="cua-driver",
                close_timeout_seconds=0.01,
            )

        if first_outcome == "cancel":
            task = asyncio.create_task(client.close())
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ProviderCleanupError) as caught:
                await client.close()
            assert_sanitized(caught.value, "provider cleanup failed")

        await client.close()
        await client.close()
        assert attempts == 2

    async def _async_result() -> ProviderToolResult:
        return result()

    asyncio.run(scenario())


@pytest.mark.parametrize("client_kind", ["in_process", "mcp"])
def test_close_does_not_overlap_uncooperative_cleanup(client_kind: str) -> None:
    async def scenario() -> None:
        attempts = 0
        cancelled = asyncio.Event()
        release = asyncio.Event()

        async def cleanup() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        cancelled.set()

        if client_kind == "in_process":
            client = InProcessProviderToolClient(
                [descriptor()], {"snapshot": lambda arguments: _async_result()},
                close_handler=cleanup,
                close_timeout_seconds=0.01,
            )
        else:
            transport = FakeMcpTransport()
            transport.close = cleanup  # type: ignore[method-assign]
            client = McpProviderToolClient(
                transport,
                provider_name="cua-driver",
                close_timeout_seconds=0.01,
            )

        with pytest.raises(ProviderCleanupError) as first:
            await client.close()
        assert_sanitized(first.value, "provider cleanup failed")
        await cancelled.wait()

        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(ProviderCleanupError) as second:
            await client.close()
        assert loop.time() - started < 0.05
        assert_sanitized(second.value, "provider cleanup failed")
        assert attempts == 1

        release.set()
        await asyncio.sleep(0)
        await client.close()
        await client.close()
        assert attempts == 2

    async def _async_result() -> ProviderToolResult:
        return result()

    asyncio.run(scenario())


@pytest.mark.parametrize("client_kind", ["in_process", "mcp"])
def test_close_waits_for_uncooperative_provider_operation_before_cleanup(
    client_kind: str,
) -> None:
    async def scenario() -> None:
        operation_cancelled = asyncio.Event()
        release = asyncio.Event()
        cleanup_attempts = 0

        async def operation() -> ProviderToolResult:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    operation_cancelled.set()
            return result("late")

        async def cleanup() -> None:
            nonlocal cleanup_attempts
            cleanup_attempts += 1

        if client_kind == "in_process":
            client = InProcessProviderToolClient(
                [descriptor()], {"snapshot": lambda arguments: operation()},
                close_handler=cleanup,
                timeout_seconds=0.01,
                close_timeout_seconds=0.01,
            )
            invoke = client.call_tool("snapshot", {})
        else:
            transport = FakeMcpTransport()

            async def list_tools(cursor: str | None = None) -> object:
                return await operation()

            transport.list_tools = list_tools  # type: ignore[method-assign]
            transport.close = cleanup  # type: ignore[method-assign]
            client = McpProviderToolClient(
                transport,
                provider_name="cua-driver",
                timeout_seconds=0.01,
                close_timeout_seconds=0.01,
            )
            invoke = client.list_tools()

        with pytest.raises(ProviderToolError) as timed_out:
            await invoke
        assert_sanitized(timed_out.value, "provider operation timed out")
        await operation_cancelled.wait()

        with pytest.raises(ProviderCleanupError) as caught:
            await client.close()
        assert_sanitized(caught.value, "provider cleanup failed")
        assert cleanup_attempts == 0

        release.set()
        await asyncio.sleep(0)
        await client.close()
        await client.close()
        assert cleanup_attempts == 1

    asyncio.run(scenario())

"""Contract tests for the portable MCP client.

The portable client drives a single ``tools/call`` over the official
Streamable HTTP MCP transport. It is the minimum surface that the
shared scenarios need today; ``list_tools`` is intentionally not
exposed as a separate helper because ``call_tool`` validates the
inventory on every call (the server-side contract requires it).
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from agent_relay import json_bounds

E2E_DIR = Path(__file__).resolve().parent / "e2e"


def _load_mcp_client() -> ModuleType:
    dotted = "tests.e2e.mcp_client"
    cached = sys.modules.get(dotted)
    if cached is not None:
        return cached
    # ``mcp_client`` imports ``EXPECTED_MCP_TOOLS`` from ``scenarios``;
    # ensure that sibling module is loaded first under its dotted name
    # so the relative-style import resolves.
    scenarios_spec = importlib.util.spec_from_file_location(
        "tests.e2e.scenarios", E2E_DIR / "scenarios.py"
    )
    if scenarios_spec and scenarios_spec.loader:
        scenarios_mod = importlib.util.module_from_spec(scenarios_spec)
        sys.modules["tests.e2e.scenarios"] = scenarios_mod
        scenarios_spec.loader.exec_module(scenarios_mod)
    spec = importlib.util.spec_from_file_location(dotted, E2E_DIR / "mcp_client.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {dotted}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = module
    spec.loader.exec_module(module)
    return module


def test_mcp_client_module_exposes_call_tool_and_explicit_tools_list() -> None:
    client = _load_mcp_client()
    assert hasattr(client, "call_tool")
    assert hasattr(client, "call_tool_async")
    assert hasattr(client.MCPClientSession, "list_tools")


def test_mcp_client_schema_limits_match_the_product_boundary() -> None:
    client = _load_mcp_client()
    assert client.MAX_JSON_BYTES == json_bounds.MAX_JSON_BYTES
    assert client.MAX_JSON_DEPTH == json_bounds.MAX_JSON_DEPTH
    assert client.MAX_JSON_NODES == json_bounds.MAX_JSON_NODES
    assert (
        client.MAX_JSON_COLLECTION_ITEMS
        == json_bounds.MAX_JSON_COLLECTION_ITEMS
    )


def test_mcp_client_accepts_ordered_announced_tool_subsets() -> None:
    client = _load_mcp_client()
    expected = client.EXPECTED_MCP_TOOLS

    assert client._valid_tool_inventory(client.SERVER_MCP_TOOLS)
    assert client._valid_tool_inventory(expected[:3])
    assert client._valid_tool_inventory(expected)
    assert not client._valid_tool_inventory(tuple(reversed(expected)))
    assert not client._valid_tool_inventory((*expected, "relay_unexpected_tool"))


def test_mcp_client_classifies_inventory_drift_without_tool_payloads() -> None:
    client = _load_mcp_client()
    expected = client.EXPECTED_MCP_TOOLS

    assert client._tool_inventory_mismatch_category(()) == "server-tool"
    assert client._tool_inventory_mismatch_category(("relay_device_status", "relay_device_status")) == "duplicate"
    assert client._tool_inventory_mismatch_category((*expected, "relay_unexpected_tool")) == "unexpected-tool"
    assert client._tool_inventory_mismatch_category((expected[0], expected[2], expected[1])) == "order"


def test_mcp_client_inventory_uses_generic_cua_public_names() -> None:
    client = _load_mcp_client()
    assert "relay_cua_list_windows" in client.EXPECTED_MCP_TOOLS
    assert "relay_cua_get_window_state" in client.EXPECTED_MCP_TOOLS
    assert "relay_cua_click" in client.EXPECTED_MCP_TOOLS
    assert "relay_cua_type_text" in client.EXPECTED_MCP_TOOLS
    assert not any("relay_computer_" in name for name in client.EXPECTED_MCP_TOOLS)


def test_mcp_client_rejects_unbounded_or_executable_tool_schemas() -> None:
    client = _load_mcp_client()
    tool = type(
        "Tool",
        (),
        {
            "name": "relay_cua_click",
            "inputSchema": {
                "type": "object",
                "properties": {"handler": {"type": "string"}},
            },
        },
    )()
    with pytest.raises(client.MCPContractError):
        client._validate_tool_schema(tool)

    deep: dict[str, object] = {"type": "object"}
    cursor = deep
    for _ in range(client.MAX_JSON_DEPTH + 1):
        child: dict[str, object] = {"type": "object"}
        cursor["properties"] = child
        cursor = child
    setattr(tool, "inputSchema", deep)
    with pytest.raises(client.MCPContractError):
        client._validate_tool_schema(tool)


def test_mcp_client_accepts_bounded_driver_schema_with_long_descriptions() -> None:
    client = _load_mcp_client()
    tool = type(
        "Tool",
        (),
        {
            "name": "relay_cua_click",
            "inputSchema": {
                "type": "object",
                "description": "x" * 1096,
                "properties": {
                    "element_token": {
                        "type": "string",
                        "description": "y" * 1096,
                    }
                },
                "additionalProperties": False,
            },
        },
    )()

    client._validate_tool_schema(tool)


def test_mcp_client_exposes_contract_error() -> None:
    client = _load_mcp_client()
    assert hasattr(client, "MCPContractError")


def test_mcp_client_does_not_import_docker_or_native_harness() -> None:
    """The portable client must be harness-agnostic."""
    client = _load_mcp_client()
    forbidden = {"docker", "container_e2e", "linux_e2e", "windows_e2e"}
    leaked: list[str] = []
    for value in client.__dict__.values():
        top = getattr(value, "__name__", None)
        if isinstance(top, str) and top.split(".", 1)[0] in forbidden:
            leaked.append(top)
        mod = getattr(value, "__module__", None)
        if mod and mod.split(".", 1)[0] in forbidden:
            leaked.append(mod)
    assert not leaked, f"leaked platform imports: {leaked}"


def test_cancelled_session_does_not_wait_for_uncooperative_transport_close() -> None:
    client = _load_mcp_client()

    class BlockingStack:
        def __init__(self) -> None:
            self.close_called = False

        async def aclose(self) -> None:
            self.close_called = True
            while True:
                try:
                    await asyncio.sleep(0.001)
                except asyncio.CancelledError:
                    continue

    class TestSession(client.MCPClientSession):
        async def __aenter__(self):
            return self

    stack = BlockingStack()
    session = TestSession("http://127.0.0.1:8123/mcp", "token")
    session._stack = stack

    async def run_cancellable_body() -> None:
        async with session:
            await asyncio.sleep(60)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(asyncio.wait_for(run_cancellable_body(), timeout=0.05))
    assert stack.close_called is False


def test_call_tool_normalizes_unavailable_transport_to_connection_error() -> None:
    """A closed loopback endpoint must remain retryable by native harnesses."""
    client = _load_mcp_client()
    with pytest.raises(ConnectionError, match="relay MCP endpoint is unavailable"):
        client.call_tool(
            mcp_url="http://127.0.0.1:1/mcp",
            control_token="token",
            tool_name="relay_device_status",
            arguments={},
            http_timeout=0.05,
            operation_timeout=0.1,
        )


def test_mcp_preflight_rejects_listening_but_unresponsive_endpoint() -> None:
    client = _load_mcp_client()

    async def exercise() -> None:
        async def blackhole(_reader, writer) -> None:
            try:
                await asyncio.sleep(1)
            finally:
                writer.close()

        server = await asyncio.start_server(blackhole, "127.0.0.1", 0)
        socket = server.sockets[0]
        port = socket.getsockname()[1]
        try:
            with pytest.raises(ConnectionError, match="relay MCP endpoint is unavailable"):
                await client._ensure_loopback_endpoint_reachable(
                    f"http://127.0.0.1:{port}/mcp",
                    0.05,
                )
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(exercise())


def test_mcp_session_call_has_a_bounded_transport_timeout() -> None:
    client = _load_mcp_client()

    class BlockingSession:
        async def send_request(self, _request, _result_type):
            await asyncio.sleep(1)

    session = client.MCPClientSession(
        "http://127.0.0.1:8123/mcp",
        "token",
        http_timeout=0.05,
    )
    session._session = BlockingSession()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(session.call("relay_device_status", {}))


def test_mcp_session_tools_list_has_a_bounded_transport_timeout() -> None:
    client = _load_mcp_client()

    class BlockingSession:
        async def list_tools(self):
            await asyncio.sleep(1)

    session = client.MCPClientSession(
        "http://127.0.0.1:8123/mcp",
        "token",
        http_timeout=0.05,
    )
    session._session = BlockingSession()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(session.list_tools())


def test_call_tool_validates_url_is_loopback() -> None:
    """``call_tool`` must refuse non-loopback URLs to preserve the
    security invariant (no non-loopback plaintext WebSocket/MCP)."""
    client = _load_mcp_client()
    with pytest.raises(ValueError):
        client.call_tool(
            mcp_url="http://0.0.0.0:8000/mcp",
            control_token="t",
            tool_name="relay_system_ping",
            arguments={},
        )


def test_call_tool_validates_tool_name_is_in_inventory() -> None:
    """Calling a tool not in the public inventory fails before any I/O."""
    client = _load_mcp_client()
    with pytest.raises(ValueError):
        client.call_tool(
            mcp_url="http://127.0.0.1:8000/mcp",
            control_token="t",
            tool_name="relay_bogus",
            arguments={},
        )


def test_call_tool_validates_arguments_is_a_dict() -> None:
    """Arguments must be a dict (closed authority surface)."""
    client = _load_mcp_client()
    with pytest.raises(ValueError):
        client.call_tool(
            mcp_url="http://127.0.0.1:8000/mcp",
            control_token="t",
            tool_name="relay_system_ping",
            arguments="not-a-dict",  # type: ignore[arg-type]
        )


def test_call_tool_validates_control_token_is_non_empty_string() -> None:
    client = _load_mcp_client()
    with pytest.raises(ValueError):
        client.call_tool(
            mcp_url="http://127.0.0.1:8000/mcp",
            control_token="",
            tool_name="relay_system_ping",
            arguments={},
        )


def test_call_tool_async_returns_when_sdk_returns_expected_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``call_tool_async`` returns the ``CallToolResult`` from the SDK."""
    client = _load_mcp_client()

    class FakeResult:
        def __init__(self) -> None:
            self.structuredContent = {"pong": True}
            self.isError = False
            self.model_extra = None

    class FakeSession:
        async def initialize(self) -> None:
            return None

        async def list_tools(self):
            class _Tools:
                tools = [
                    type(
                        "T",
                        (),
                        {
                            "name": n,
                            "inputSchema": {
                                "type": "object",
                                "properties": {},
                                "additionalProperties": False,
                            },
                        },
                    )()
                    for n in (
                        "relay_device_status",
                        "relay_system_ping",
                        "relay_terminal_exec",
                        "relay_cua_list_windows",
                        "relay_cua_get_window_state",
                        "relay_cua_click",
                        "relay_cua_type_text",
                    )
                ]

            return _Tools()

        async def send_request(self, request, result_type):
            return FakeResult()

    class FakeStreamableClient:
        async def __aenter__(self):
            return (None, None, None)

        async def __aexit__(self, exc_type, exc, tb):
            return None

    async def fake_streamable_client(url, http_client=None):
        return FakeStreamableClient()

    # ``streamable_http_client`` is normally an async context manager
    # factory. We patch it with an async-context-manager class so the
    # ``async with`` block in the client can use it directly.
    class _FakeStreamableClientCM:
        async def __aenter__(self):
            return (None, None, None)

        async def __aexit__(self, exc_type, exc, tb):
            return None

    def _factory(url, http_client=None):
        return _FakeStreamableClientCM()

    class _FakeClientSession:
        def __init__(self, *args, **kwargs) -> None:
            self.session = FakeSession()

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(client, "streamable_http_client", _factory)
    monkeypatch.setattr(client, "ClientSession", _FakeClientSession)

    async def _fake_endpoint_reachable(_mcp_url: str, _timeout: float) -> None:
        return None

    monkeypatch.setattr(client, "_ensure_loopback_endpoint_reachable", _fake_endpoint_reachable)

    import asyncio

    result = asyncio.run(
        client.call_tool_async(
            mcp_url="http://127.0.0.1:8123/mcp",
            control_token="secret-token",
            tool_name="relay_system_ping",
            arguments={},
        )
    )
    assert result.structuredContent == {"pong": True}

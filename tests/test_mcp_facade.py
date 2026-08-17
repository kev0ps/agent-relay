from __future__ import annotations

import asyncio
import socket
from datetime import timedelta

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CancelledNotification, CancelledNotificationParams

from agent_relay.mcp_facade import (
    _close_tool_input_schemas,
    create_mcp_facade,
)
from agent_relay.output_models import ProviderToolResult
from agent_relay.protocol import (
    AgentResult,
    Capabilities,
    InvokeMessage,
    Register,
)
from agent_relay.provider_tools import ProviderToolDescriptor
from agent_relay.registry import (
    DeviceBusyError,
    DeviceOfflineError,
    LateResponseError,
    RelayRegistry,
    RemoteAgentError,
    UnknownDeviceError,
    UnsupportedToolError,
)
from agent_relay.server import RelaySettings, create_app


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


class FakeSocket:
    async def send_json(self, message: object) -> None:
        pass


class BlockingSocket:
    def __init__(self) -> None:
        self.messages: list[object] = []
        self.invoke_seen = asyncio.Event()
        self.cancel_seen = asyncio.Event()

    async def send_json(self, message: object) -> None:
        self.messages.append(message)
        if isinstance(message, dict) and message.get("type") == "invoke":
            self.invoke_seen.set()
        elif isinstance(message, dict) and message.get("type") == "cancel":
            self.cancel_seen.set()


class StubRegistry:
    def __init__(self, result: ProviderToolResult | BaseException) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []

    async def invoke(self, *args: object) -> ProviderToolResult:
        self.calls.append(args)
        if isinstance(self.result, BaseException):
            raise self.result
        assert isinstance(self.result, ProviderToolResult)
        return self.result


def test_dynamic_facade_only_lists_announced_agent_tools() -> None:
    async def scenario() -> None:
        registry = RelayRegistry(agent_token="agent-token")
        mcp = create_mcp_facade(
            registry=registry,
            timeout_seconds=1,
            only_announced=True,
        )
        assert [tool.name for tool in mcp._tool_manager.list_tools()] == [
            "relay_device_status"
        ]
        socket = FakeSocket()
        registered = await registry.register(
            socket,
            Register(version=1, type="register", device_id="one"),
        )
        assert registered.device_id == "one"
        await registry.set_capabilities(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                tools=["system.ping"],
                descriptors=[
                    ProviderToolDescriptor(
                        provider_name="system",
                        tool_name="ping",
                        public_name="relay_system_ping",
                        description="fixed local health check",
                        input_schema={
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                        risk="read_only",
                    )
                ],
            ),
        )
        assert [tool.name for tool in mcp._tool_manager.list_tools()] == [
            "relay_device_status",
            "relay_system_ping",
        ]

    run(scenario())


def test_dynamic_facade_publishes_cua_browser_schema_without_static_wrapper() -> None:
    async def scenario() -> None:
        registry = RelayRegistry(agent_token="agent-token")
        mcp = create_mcp_facade(
            registry=registry,
            timeout_seconds=1,
            only_announced=True,
        )
        socket = FakeSocket()
        await registry.register(
            socket,
            Register(version=1, type="register", device_id="cua-one"),
        )
        descriptor = ProviderToolDescriptor(
            provider_name="cua",
            tool_name="browser_type",
            public_name="relay_cua_browser_type",
            description="CUA browser type",
            input_schema={
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "tab_id": {"type": "string"},
                    "ref": {"type": "string"},
                    "text": {"type": "string"},
                },
                "additionalProperties": False,
            },
            risk="interaction",
        )
        await registry.set_capabilities(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                tools=["cua.browser_type"],
                descriptors=[descriptor],
            ),
        )
        tools = mcp._tool_manager.list_tools()
        assert [tool.name for tool in tools] == [
            "relay_device_status",
            "relay_cua_browser_type",
        ]
        cua_tool = tools[1]
        assert "target_id" in cua_tool.parameters["properties"]
        assert "element_id" not in cua_tool.parameters["properties"]

    run(scenario())


def test_tool_discovery_is_exact_and_closed() -> None:
    async def scenario() -> None:
        registry = RelayRegistry(device_id="one", agent_token="agent-token")
        mcp = create_mcp_facade(registry=registry, device_id="one", timeout_seconds=1)
        async with create_connected_server_and_client_session(mcp) as session:
            tools = (await session.list_tools()).tools

        assert [tool.name for tool in tools] == [
            "relay_device_status",
            "relay_system_ping",
            "relay_terminal_exec",
        ]
        by_name = {tool.name: tool for tool in tools}
        for name in ("relay_device_status", "relay_system_ping"):
            assert by_name[name].inputSchema == {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            }
            assert by_name[name].outputSchema is not None
            assert by_name[name].outputSchema["additionalProperties"] is False
        terminal_schema = by_name["relay_terminal_exec"].inputSchema
        assert terminal_schema["additionalProperties"] is False
        assert terminal_schema["required"] == ["command_id"]
        assert terminal_schema["properties"]["command_id"]["enum"] == [
            "pwd",
            "whoami",
            "python_version",
            "git_status",
            "git_branch",
        ]

    run(scenario())


@pytest.mark.integration
def test_public_mcp_call_cancellation_sends_one_cancel_and_releases_request() -> None:
    async def scenario() -> None:
        agent_socket = BlockingSocket()
        relay_request_id: str | None = None
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        app = create_app(
            RelaySettings(
                device_id="one",
                agent_token="agent-token",
                mcp_token="control-token",
                max_timeout_seconds=10,
            )
        )
        registry = app.state.registry
        await registry.register(
            agent_socket,
            Register(version=1, type="register", device_id="one"),
        )
        await registry.set_capabilities(
            agent_socket,
            Capabilities(
                version=1,
                type="capabilities",
                tools=["terminal.exec"],
                descriptors=[
                    ProviderToolDescriptor(
                        provider_name="terminal",
                        tool_name="exec",
                        public_name="relay_terminal_exec",
                        description="fixed allowlisted terminal command",
                        input_schema={
                            "type": "object",
                            "properties": {
                                "command_id": {
                                    "type": "string",
                                    "enum": [
                                        "pwd",
                                        "whoami",
                                        "python_version",
                                        "git_status",
                                        "git_branch",
                                    ],
                                }
                            },
                            "required": ["command_id"],
                            "additionalProperties": False,
                        },
                        risk="interaction",
                    )
                ],
            ),
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="critical",
            )
        )
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started
            async with httpx.AsyncClient(
                headers={"Authorization": "Bearer control-token"},
            ) as http_client:
                async with streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp",
                    http_client=http_client,
                    terminate_on_close=False,
                ) as (read_stream, write_stream, _):
                    async with ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=timedelta(seconds=10),
                    ) as session:
                        await session.initialize()
                        call = asyncio.create_task(
                            session.call_tool(
                                "relay_terminal_exec", {"command_id": "pwd"}
                            )
                        )
                        await asyncio.wait_for(agent_socket.invoke_seen.wait(), timeout=1)
                        # The SDK does not expose the request id; call_tool is
                        # the next request after initialize in this isolated session.
                        request_id = session._request_id - 1  # pyright: ignore[reportPrivateUsage]
                        await session.send_notification(
                            CancelledNotification(
                                params=CancelledNotificationParams(
                                    requestId=request_id,
                                    reason="test cancellation",
                                )
                            )  # type: ignore[arg-type]
                        )
                        await asyncio.wait_for(agent_socket.cancel_seen.wait(), timeout=2)
                        call.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await call
                        relay_request_id = next(
                            message["request_id"]
                            for message in agent_socket.messages
                            if isinstance(message, dict)
                            and message.get("type") == "invoke"
                        )
            await asyncio.wait_for(agent_socket.cancel_seen.wait(), timeout=2)
        finally:
            server.should_exit = True
            server.force_exit = True
            await asyncio.wait_for(server_task, timeout=2)

        assert registry.pending_count == 0
        assert relay_request_id is not None
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_result(
                AgentResult(
                    version=2,
                    type="result",
                    request_id=relay_request_id,
                    result=ProviderToolResult(
                        content=[], structuredContent={"late": True}
                    ),
                )
            )
        assert [
            message["type"]
            for message in agent_socket.messages
            if isinstance(message, dict)
        ] == ["invoke", "cancel"]

    run(scenario())


def test_closed_schema_compatibility_helper_fails_clearly_on_sdk_drift() -> None:
    class UnsupportedMCP:
        pass

    with pytest.raises(RuntimeError, match="^unsupported MCP SDK tool schema API$"):
        _close_tool_input_schemas(UnsupportedMCP())  # type: ignore[arg-type]


def test_status_reports_offline_and_online_safe_state() -> None:
    async def scenario() -> None:
        registry = RelayRegistry(device_id="one", agent_token="agent-token")
        mcp = create_mcp_facade(registry=registry, device_id="one", timeout_seconds=1)
        async with create_connected_server_and_client_session(mcp) as session:
            offline = await session.call_tool("relay_device_status", {})
            socket = FakeSocket()
            await registry.register(
                socket,
                Register(
                    version=1,
                    type="register",
                    device_id="one",
                ),
            )
            await registry.set_capabilities(
                socket,
                Capabilities(
                    version=1,
                    type="capabilities",
                    tools=["terminal.exec", "system.ping"],
                ),
            )
            online = await session.call_tool("relay_device_status", {})

        assert offline.isError is False
        assert offline.structuredContent == {
            "device_id": "one",
            "connected": False,
            "capabilities": [],
            "invocation_state": "idle",
            "progress": None,
            "heartbeat_age_seconds": None,
        }
        assert online.isError is False
        assert online.structuredContent is not None
        assert online.structuredContent["device_id"] == "one"
        assert online.structuredContent["connected"] is True
        assert online.structuredContent["capabilities"] == [
            "system.ping",
            "terminal.exec",
        ]
        assert set(online.structuredContent) == {
            "device_id",
            "connected",
            "capabilities",
            "invocation_state",
            "progress",
            "heartbeat_age_seconds",
        }

    run(scenario())


@pytest.mark.parametrize(
    ("tool_name", "arguments", "result"),
    [
        ("relay_system_ping", {}, {"pong": True}),
        *[
            (
                "relay_terminal_exec",
                {"command_id": command_id},
                {
                    "command_id": command_id,
                    "stdout": "output",
                    "stderr": "",
                    "exit_code": 0,
                    "timed_out": False,
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                },
            )
            for command_id in (
                "pwd",
                "whoami",
                "python_version",
                "git_status",
                "git_branch",
            )
        ],
    ],
)
def test_invocation_tools_return_structured_output_and_fixed_parameters(
    tool_name: str, arguments: dict[str, object], result: dict[str, object]
) -> None:
    async def scenario() -> None:
        registry = StubRegistry(
            ProviderToolResult(content=[], structuredContent=result)
        )
        mcp = create_mcp_facade(  # type: ignore[arg-type]
            registry=registry, device_id="one", timeout_seconds=2.5
        )
        async with create_connected_server_and_client_session(mcp) as session:
            response = await session.call_tool(tool_name, arguments)

        assert response.isError is False
        assert response.structuredContent == result
        assert len(registry.calls) == 1
        device_id, message, timeout = registry.calls[0]
        assert device_id == "one"
        assert type(message) is InvokeMessage
        assert message.version == 2
        assert message.request_id
        assert message.tool_name == (
            "system.ping" if tool_name == "relay_system_ping" else "terminal.exec"
        )
        assert message.arguments == arguments
        assert timeout == 2.5

    run(scenario())


def test_dynamic_facade_publishes_selected_cua_descriptor() -> None:
    async def scenario() -> None:
        registry = RelayRegistry(agent_token="agent-token")
        mcp = create_mcp_facade(
            registry=registry,
            timeout_seconds=1,
            only_announced=True,
        )
        socket = FakeSocket()
        await registry.register(
            socket,
            Register(version=1, type="register", device_id="cua-one"),
        )
        await registry.set_capabilities(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                tools=["cua.click"],
                descriptors=[
                    ProviderToolDescriptor(
                        provider_name="cua",
                        tool_name="click",
                        public_name="relay_cua_click",
                        description="provider-native CUA tool: click",
                        input_schema={
                            "type": "object",
                            "properties": {
                                "target": {"type": "string", "minLength": 1}
                            },
                            "required": ["target"],
                            "additionalProperties": False,
                        },
                        risk="interaction",
                    )
                ],
            ),
        )
        tools = mcp._tool_manager.list_tools()
        assert [tool.name for tool in tools] == [
            "relay_device_status",
            "relay_cua_click",
        ]
        tool = tools[1]
        assert tool.parameters["additionalProperties"] is False
        assert set(tool.parameters["properties"]) == {"target"}

    run(scenario())



@pytest.mark.parametrize(
    ("error", "safe_message"),
    [
        (UnknownDeviceError("sensitive"), "unknown device"),
        (DeviceOfflineError("sensitive"), "device is offline"),
        (DeviceBusyError("sensitive"), "device is busy"),
        (UnsupportedToolError("sensitive"), "device does not support this capability"),
        (TimeoutError("sensitive"), "device invocation timed out"),
        (RemoteAgentError("secret-code", "sensitive"), "device invocation failed"),
    ],
)
def test_expected_relay_failures_are_safe_mcp_tool_errors(
    error: BaseException, safe_message: str
) -> None:
    async def scenario() -> None:
        registry = StubRegistry(error)
        mcp = create_mcp_facade(  # type: ignore[arg-type]
            registry=registry, device_id="one", timeout_seconds=1
        )
        async with create_connected_server_and_client_session(mcp) as session:
            response = await session.call_tool("relay_system_ping", {})

        assert response.isError is True
        assert safe_message in response.content[0].text  # type: ignore[union-attr]
        assert "sensitive" not in response.content[0].text  # type: ignore[union-attr]
        assert "secret-code" not in response.content[0].text  # type: ignore[union-attr]

    run(scenario())



@pytest.mark.parametrize("tool_name", ["relay_device_status", "relay_system_ping"])
def test_unexpected_tool_failures_never_reach_mcp_clients(tool_name: str) -> None:
    class ExplodingRegistry(StubRegistry):
        async def status_snapshot(self) -> object:
            raise RuntimeError("Bearer UNEXPECTED_SECRET")

    async def scenario() -> None:
        registry = ExplodingRegistry(RuntimeError("Bearer UNEXPECTED_SECRET"))
        mcp = create_mcp_facade(  # type: ignore[arg-type]
            registry=registry, device_id="one", timeout_seconds=1
        )
        async with create_connected_server_and_client_session(mcp) as session:
            response = await session.call_tool(tool_name, {})

        assert response.isError is True
        assert "internal relay error" in response.content[0].text  # type: ignore[union-attr]
        assert "UNEXPECTED_SECRET" not in response.content[0].text  # type: ignore[union-attr]

    run(scenario())


def test_terminal_command_enum_and_extra_arguments_are_rejected() -> None:
    async def scenario() -> None:
        registry = StubRegistry(ProviderToolResult(content=[], structuredContent={}))
        mcp = create_mcp_facade(  # type: ignore[arg-type]
            registry=registry, device_id="one", timeout_seconds=1
        )
        async with create_connected_server_and_client_session(mcp) as session:
            invalid = await session.call_tool(
                "relay_terminal_exec", {"command_id": "arbitrary"}
            )
            extra = await session.call_tool(
                "relay_system_ping", {"timeout_seconds": 999}
            )

        assert invalid.isError is True
        assert extra.isError is True
        assert registry.calls == []

    run(scenario())


@pytest.mark.integration
def test_official_streamable_http_client_uses_authenticated_canonical_mcp_url() -> None:
    async def scenario() -> None:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        app = create_app(
            RelaySettings(
                device_id="one",
                agent_token="agent-placeholder",
                mcp_token="control-placeholder",
            )
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="critical",
            )
        )
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started
            async with httpx.AsyncClient(
                headers={"Authorization": "Bearer control-placeholder"},
            ) as http_client:
                async with streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp",
                    http_client=http_client,
                    terminate_on_close=False,
                ) as (read_stream, write_stream, _):
                    async with ClientSession(read_stream, write_stream) as session:
                        initialized = await session.initialize()
                        tools = (await session.list_tools()).tools
                        status = await session.call_tool("relay_device_status", {})
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=2)

        assert initialized.serverInfo.name == "Agent Relay"
        assert [tool.name for tool in tools] == ["relay_device_status"]
        assert status.isError is False
        assert status.structuredContent is not None
        assert status.structuredContent["connected"] is False

    run(scenario())

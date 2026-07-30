from __future__ import annotations

import asyncio
import json
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
from agent_relay.output_models import (
    BrowserActionOutput,
    BrowserElementOutput,
    BrowserPageOutput,
    BrowserTabOutput,
    BrowserTabsOutput,
    ComputerActionOutput,
    ComputerCaptureOutput,
    ComputerElementOutput,
)
from agent_relay.protocol import (
    MAX_BROWSER_ELEMENT_ID_LENGTH,
    MAX_BROWSER_ELEMENT_VALUE_LENGTH,
    MAX_BROWSER_ELEMENTS,
    MAX_BROWSER_NAME_LENGTH,
    MAX_BROWSER_PAGE_TEXT_LENGTH,
    MAX_BROWSER_ROLE_LENGTH,
    MAX_BROWSER_TAB_ID_LENGTH,
    MAX_BROWSER_TABS,
    MAX_BROWSER_TITLE_LENGTH,
    MAX_BROWSER_URL_LENGTH,
    MAX_COMPUTER_ELEMENT_ID_LENGTH,
    MAX_COMPUTER_ELEMENT_VALUE_LENGTH,
    MAX_COMPUTER_ELEMENTS,
    MAX_COMPUTER_NAME_LENGTH,
    MAX_COMPUTER_ROLE_LENGTH,
    MAX_RESULT_JSON_BYTES,
    AgentResult,
    BrowserClickInvoke,
    BrowserFillInvoke,
    BrowserListTabsInvoke,
    BrowserNavigateInvoke,
    BrowserReadPageInvoke,
    Capabilities,
    ComputerCaptureInvoke,
    ComputerClickInvoke,
    ComputerTypeInvoke,
    Register,
    SystemPingInvoke,
    TerminalExecInvoke,
)
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
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []

    async def invoke(self, *args: object) -> dict[str, object]:
        self.calls.append(args)
        if isinstance(self.result, BaseException):
            raise self.result
        assert isinstance(self.result, dict)
        return self.result


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
            "relay_browser_list_tabs",
            "relay_browser_navigate",
            "relay_browser_read_page",
            "relay_browser_fill",
            "relay_browser_click",
            "relay_computer_capture",
            "relay_computer_click",
            "relay_computer_type",
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
        assert by_name["relay_terminal_exec"].outputSchema is not None
        assert (
            by_name["relay_terminal_exec"].outputSchema["additionalProperties"] is False
        )

        for name in ("relay_browser_list_tabs", "relay_browser_read_page"):
            assert by_name[name].inputSchema == {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            }
        expected_fields = {
            "relay_browser_navigate": {"url"},
            "relay_browser_fill": {"element_id", "value"},
            "relay_browser_click": {"element_id"},
        }
        for name, fields in expected_fields.items():
            schema = by_name[name].inputSchema
            assert schema["additionalProperties"] is False
            assert set(schema["properties"]) == fields
            assert set(schema["required"]) == fields
            for field in fields:
                assert schema["properties"][field]["minLength"] == 1
                assert schema["properties"][field]["maxLength"] > 1

        for name in set(expected_fields) | {
            "relay_browser_list_tabs",
            "relay_browser_read_page",
        }:
            output = by_name[name].outputSchema
            assert output is not None
            assert output["additionalProperties"] is False

        tabs = by_name["relay_browser_list_tabs"].outputSchema
        assert tabs["type"] == "object" and tabs["required"] == ["tabs"]
        assert tabs["properties"]["tabs"]["maxItems"] > 0
        assert tabs["$defs"]["BrowserTabOutput"]["additionalProperties"] is False
        page = by_name["relay_browser_read_page"].outputSchema
        assert page["properties"]["text"]["maxLength"] > 0
        assert page["properties"]["elements"]["maxItems"] > 0
        assert page["$defs"]["BrowserElementOutput"]["additionalProperties"] is False

        capture = by_name["relay_computer_capture"]
        assert capture.inputSchema == {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        assert capture.outputSchema is not None
        assert capture.outputSchema["additionalProperties"] is False
        assert set(capture.outputSchema["properties"]) == {
            "app", "window_title", "generation", "elements"
        }
        assert capture.outputSchema["properties"]["elements"]["maxItems"] > 0
        element = capture.outputSchema["$defs"]["ComputerElementOutput"]
        assert element["additionalProperties"] is False
        assert set(element["properties"]) == {
            "element_id", "role", "name", "value", "enabled"
        }
        assert set(element["required"]) == {
            "element_id", "role", "name", "value", "enabled"
        }

        for name, fields in {
            "relay_computer_click": {"element_id"},
            "relay_computer_type": {"element_id", "text"},
        }.items():
            tool = by_name[name]
            assert tool.inputSchema["additionalProperties"] is False
            assert set(tool.inputSchema["properties"]) == fields
            assert set(tool.inputSchema["required"]) == fields
            assert tool.outputSchema is not None
            assert tool.outputSchema["additionalProperties"] is False
            assert set(tool.outputSchema["properties"]) == {
                "success", "generation", "element_id"
            }
            assert set(tool.outputSchema["required"]) == {
                "success", "generation", "element_id"
            }

        text_schema = by_name["relay_computer_type"].inputSchema["properties"]["text"]
        assert text_schema["minLength"] == 1
        assert text_schema["maxLength"] > 1
        assert "pattern" in text_schema

        assert ComputerElementOutput.model_config["extra"] == "forbid"
        assert ComputerCaptureOutput.model_config["strict"] is True
        assert ComputerActionOutput.model_config["strict"] is True

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
                control_token="control-token",
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
                    version=1,
                    type="result",
                    request_id=relay_request_id,
                    result={"late": True},
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
        registry = StubRegistry(result)
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
        expected_type = (
            SystemPingInvoke if tool_name == "relay_system_ping" else TerminalExecInvoke
        )
        assert type(message) is expected_type
        assert message.request_id
        assert message.tool == (
            "system.ping" if tool_name == "relay_system_ping" else "terminal.exec"
        )
        if isinstance(message, TerminalExecInvoke):
            assert message.command_id == arguments["command_id"]
        assert timeout == 2.5

    run(scenario())


@pytest.mark.parametrize(
    ("tool_name", "arguments", "result", "expected_type"),
    [
        ("relay_browser_list_tabs", {}, {"tabs": []}, BrowserListTabsInvoke),
        (
            "relay_browser_navigate",
            {"url": "https://example.test"},
            {
                "tab_id": "tab-1",
                "element_id": None,
                "url": "https://example.test",
                "title": "Example",
                "success": True,
            },
            BrowserNavigateInvoke,
        ),
        (
            "relay_browser_read_page",
            {},
            {
                "tab_id": "tab-1",
                "title": "Example",
                "url": "https://example.test",
                "text": "Hello",
                "elements": [],
            },
            BrowserReadPageInvoke,
        ),
        (
            "relay_browser_fill",
            {"element_id": "field-1", "value": "hello"},
            {
                "tab_id": "tab-1",
                "element_id": "field-1",
                "url": "https://example.test",
                "title": "Example",
                "success": True,
            },
            BrowserFillInvoke,
        ),
        (
            "relay_browser_click",
            {"element_id": "button-1"},
            {
                "tab_id": "tab-1",
                "element_id": "button-1",
                "url": "https://example.test",
                "title": "Example",
                "success": True,
            },
            BrowserClickInvoke,
        ),
    ],
)
def test_browser_tools_map_to_exact_typed_invokes(
    tool_name: str,
    arguments: dict[str, object],
    result: dict[str, object],
    expected_type: type[object],
) -> None:
    async def scenario() -> None:
        registry = StubRegistry(result)
        mcp = create_mcp_facade(registry=registry, device_id="one", timeout_seconds=2.5)  # type: ignore[arg-type]
        async with create_connected_server_and_client_session(mcp) as session:
            response = await session.call_tool(tool_name, arguments)
        assert response.isError is False
        assert response.structuredContent == result
        _, message, timeout = registry.calls[0]
        assert type(message) is expected_type
        assert message.request_id
        assert timeout == 2.5
        for key, value in arguments.items():
            assert getattr(message, key) == value

    run(scenario())


@pytest.mark.parametrize(
    ("tool_name", "arguments", "result", "expected_type"),
    [
        (
            "relay_computer_capture",
            {},
            {
                "app": "fixture",
                "window_title": "Fixture",
                "generation": "generation-1",
                "elements": [],
            },
            ComputerCaptureInvoke,
        ),
        (
            "relay_computer_click",
            {"element_id": "opaque-1"},
            {"success": True, "generation": "generation-1", "element_id": "opaque-1"},
            ComputerClickInvoke,
        ),
        (
            "relay_computer_type",
            {"element_id": "opaque-1", "text": "hello"},
            {"success": True, "generation": "generation-1", "element_id": "opaque-1"},
            ComputerTypeInvoke,
        ),
    ],
)
def test_computer_tools_map_to_exact_typed_invokes(
    tool_name: str,
    arguments: dict[str, object],
    result: dict[str, object],
    expected_type: type[object],
) -> None:
    async def scenario() -> None:
        registry = StubRegistry(result)
        mcp = create_mcp_facade(registry=registry, device_id="one", timeout_seconds=2.5)  # type: ignore[arg-type]
        async with create_connected_server_and_client_session(mcp) as session:
            response = await session.call_tool(tool_name, arguments)
        assert response.isError is False
        assert response.structuredContent == result
        _, message, timeout = registry.calls[0]
        assert type(message) is expected_type
        assert timeout == 2.5
        for key, value in arguments.items():
            assert getattr(message, key) == value

    run(scenario())


def test_computer_type_unicode_control_policy_applies_at_mcp_boundary() -> None:
    async def scenario() -> None:
        registry = StubRegistry(
            {"success": True, "generation": "g", "element_id": "opaque-1"}
        )
        mcp = create_mcp_facade(  # type: ignore[arg-type]
            registry=registry, device_id="one", timeout_seconds=1
        )
        async with create_connected_server_and_client_session(mcp) as session:
            for text in ("a\u0085b", "a\u202eb"):
                response = await session.call_tool(
                    "relay_computer_type", {"element_id": "opaque-1", "text": text}
                )
                assert response.isError is True
            accepted = await session.call_tool(
                "relay_computer_type", {"element_id": "opaque-1", "text": "ok \U0001f680"}
            )
        assert accepted.isError is False
        assert len(registry.calls) == 1

    run(scenario())


def test_computer_outputs_reject_unbounded_or_arbitrary_data() -> None:
    valid_element = {
        "element_id": "opaque-1",
        "role": "textbox",
        "name": "Name",
        "value": None,
        "enabled": True,
    }
    with pytest.raises(Exception):
        ComputerElementOutput.model_validate(valid_element | {"coordinates": [1, 2]})
    with pytest.raises(Exception):
        ComputerCaptureOutput.model_validate(
            {
                "app": "fixture",
                "window_title": "Fixture",
                "generation": "generation-1",
                "elements": [valid_element] * (MAX_COMPUTER_ELEMENTS + 1),
            }
        )
    with pytest.raises(Exception):
        ComputerActionOutput.model_validate(
            {"success": True, "generation": "generation-1", "element_id": None}
        )


def test_browser_output_models_reject_unknown_and_oversized_nested_data() -> None:
    with pytest.raises(Exception):
        BrowserTabsOutput.model_validate({"tabs": [], "extra": True})
    with pytest.raises(Exception):
        BrowserElementOutput.model_validate(
            {
                "element_id": "e",
                "role": "textbox",
                "name": "n",
                "value": None,
                "editable": True,
                "enabled": True,
                "extra": True,
            }
        )
    page_schema = BrowserPageOutput.model_json_schema()
    text_max = page_schema["properties"]["text"]["maxLength"]
    with pytest.raises(Exception):
        BrowserPageOutput.model_validate(
            {
                "tab_id": "t",
                "title": "t",
                "url": "u",
                "text": "x" * (text_max + 1),
                "elements": [],
            }
        )
    with pytest.raises(Exception):
        BrowserActionOutput.model_validate(
            {
                "tab_id": "t",
                "element_id": None,
                "url": "u",
                "title": "t",
                "success": True,
                "extra": True,
            }
        )


def test_worst_case_browser_outputs_fit_protocol_result_budget() -> None:
    astral = "\U00010000"
    tab = BrowserTabOutput(
        tab_id=astral * MAX_BROWSER_TAB_ID_LENGTH,
        title=astral * MAX_BROWSER_TITLE_LENGTH,
        url=astral * MAX_BROWSER_URL_LENGTH,
    )
    element = BrowserElementOutput(
        element_id=astral * MAX_COMPUTER_ELEMENT_ID_LENGTH,
        role=astral * MAX_BROWSER_ROLE_LENGTH,
        name=astral * MAX_BROWSER_NAME_LENGTH,
        value=astral * MAX_COMPUTER_ELEMENT_VALUE_LENGTH,
        editable=True,
        enabled=True,
    )
    outputs = (
        BrowserTabsOutput(tabs=[tab] * MAX_BROWSER_TABS),
        BrowserPageOutput(
            tab_id=tab.tab_id,
            title=tab.title,
            url=tab.url,
            text=astral * MAX_BROWSER_PAGE_TEXT_LENGTH,
            elements=[element] * MAX_BROWSER_ELEMENTS,
        ),
        BrowserActionOutput(
            tab_id=tab.tab_id,
            element_id=element.element_id,
            url=tab.url,
            title=tab.title,
            success=True,
        ),
    )
    for output in outputs:
        encoded = json.dumps(
            output.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
        ).encode()
    assert len(encoded) < MAX_RESULT_JSON_BYTES


def test_worst_case_computer_output_fits_protocol_result_budget() -> None:
    astral = "\U00010000"
    element = ComputerElementOutput(
        element_id=astral * MAX_BROWSER_ELEMENT_ID_LENGTH,
        role=astral * MAX_COMPUTER_ROLE_LENGTH,
        name=astral * MAX_COMPUTER_NAME_LENGTH,
        value=astral * MAX_BROWSER_ELEMENT_VALUE_LENGTH,
        enabled=True,
    )
    capture = ComputerCaptureOutput(
        app=astral * 128,
        window_title=astral * 256,
        generation=astral * 128,
        elements=[element] * MAX_COMPUTER_ELEMENTS,
    )
    encoded = json.dumps(capture.model_dump(), ensure_ascii=False, separators=(",", ":")).encode()
    assert len(encoded) < MAX_RESULT_JSON_BYTES


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


def test_unadvertised_browser_capability_returns_safe_mcp_error() -> None:
    async def scenario() -> None:
        registry = StubRegistry(UnsupportedToolError("browser.click"))
        mcp = create_mcp_facade(  # type: ignore[arg-type]
            registry=registry, device_id="one", timeout_seconds=1
        )
        async with create_connected_server_and_client_session(mcp) as session:
            response = await session.call_tool(
                "relay_browser_click", {"element_id": "button-1"}
            )
        assert response.isError is True
        assert "device does not support this capability" in response.content[0].text  # type: ignore[union-attr]
        assert "browser.click" not in response.content[0].text  # type: ignore[union-attr]

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
        registry = StubRegistry({})
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
                control_token="control-placeholder",
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
        assert [tool.name for tool in tools] == [
            "relay_device_status",
            "relay_system_ping",
            "relay_terminal_exec",
            "relay_browser_list_tabs",
            "relay_browser_navigate",
            "relay_browser_read_page",
            "relay_browser_fill",
            "relay_browser_click",
            "relay_computer_capture",
            "relay_computer_click",
            "relay_computer_type",
        ]
        assert status.isError is False
        assert status.structuredContent is not None
        assert status.structuredContent["connected"] is False

    run(scenario())

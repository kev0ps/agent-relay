from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult

from agent_relay.mcp_dynamic_registry import DynamicToolManager
from agent_relay.mcp_facade import create_mcp_facade
from agent_relay.output_models import ProviderTextContent, ProviderToolResult
from agent_relay.protocol import InvokeMessage
from agent_relay.provider_tools import ProviderToolDescriptor

CUA_TOOL_NAMES = (
    "list_apps",
    "list_windows",
    "get_window_state",
    "get_accessibility_tree",
    "get_desktop_state",
    "get_screen_size",
    "get_cursor_position",
    "get_config",
    "get_recording_state",
    "get_agent_cursor_state",
    "launch_app",
    "kill_app",
    "bring_to_front",
    "click",
    "double_click",
    "right_click",
    "drag",
    "type_text",
    "press_key",
    "hotkey",
    "set_value",
    "scroll",
    "move_cursor",
    "zoom",
    "page",
    "get_browser_state",
    "browser_prepare",
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_dialog",
    "browser_set_input_files",
    "browser_download",
    "browser_pointer",
    "start_recording",
    "stop_recording",
    "replay_trajectory",
    "set_config",
    "start_session",
    "end_session",
    "set_agent_cursor_enabled",
    "set_agent_cursor_motion",
    "check_permissions",
    "health_report",
    "check_for_update",
    "install_ffmpeg",
    "verify_state",
    "set_agent_cursor_theme",
    "escalate_session",
    "get_session_state",
)


class _Registry:
    def __init__(self, descriptors: Mapping[str, ProviderToolDescriptor]) -> None:
        self.descriptors = dict(descriptors)
        self.calls: list[tuple[str | None, InvokeMessage, float]] = []
        self.result = ProviderToolResult(
            content=[
                {"type": "text", "text": "clicked"},
                {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
            ],
            structuredContent={"accepted": True},
        )

    @property
    def announced_descriptors(self) -> dict[str, ProviderToolDescriptor]:
        return dict(self.descriptors)

    async def invoke(
        self,
        device_id: str | None,
        message: InvokeMessage,
        timeout_seconds: float,
    ) -> ProviderToolResult:
        self.calls.append((device_id, message, timeout_seconds))
        return self.result


def _descriptor(
    tool_name: str,
    *,
    public_name: str | None = None,
    risk: str = "interaction",
) -> ProviderToolDescriptor:
    return ProviderToolDescriptor(
        provider_name="cua-driver",
        tool_name=tool_name,
        public_name=public_name or f"relay_cua_driver_{tool_name}",
        description=f"CUA {tool_name}",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string", "maxLength": 32}},
            "required": ["value"],
            "additionalProperties": False,
        },
        risk=risk,  # type: ignore[arg-type]
    )


def test_dynamic_manager_publishes_only_selected_provider_descriptors() -> None:
    selected = {
        f"cua-driver.{name}": _descriptor(name)
        for name in (CUA_TOOL_NAMES[0], CUA_TOOL_NAMES[-1])
    }
    registry = _Registry(selected)
    manager = DynamicToolManager(
        registry=registry,
        timeout_seconds=3.5,
        device_id="device-1",
    )

    tools = manager.list_tools()

    assert len(CUA_TOOL_NAMES) == 50
    assert {tool.name for tool in tools} == {
        selected_descriptor.public_name for selected_descriptor in selected.values()
    }
    assert all(not tool.name.endswith("_invoke") for tool in tools)
    first = next(tool for tool in tools if tool.name.endswith(CUA_TOOL_NAMES[0]))
    assert first.description == f"CUA {CUA_TOOL_NAMES[0]}"
    assert first.parameters == next(iter(selected.values())).input_schema
    assert first.meta == {"risk": "interaction"}


def test_dynamic_facade_exposes_selected_tool_through_mcp_session() -> None:
    async def scenario() -> None:
        descriptor = _descriptor("click", public_name="relay_cua_driver_click")
        registry = _Registry({"cua-driver.click": descriptor})
        mcp = create_mcp_facade(
            registry=registry,  # type: ignore[arg-type]
            timeout_seconds=4.0,
            only_announced=True,
        )

        async with create_connected_server_and_client_session(mcp) as session:
            tools = (await session.list_tools()).tools
            assert [tool.name for tool in tools] == [
                "relay_device_status",
                descriptor.public_name,
            ]
            assert next(
                tool for tool in tools if tool.name == descriptor.public_name
            ).inputSchema == descriptor.input_schema

            result = await session.call_tool(
                descriptor.public_name,
                {"value": "target"},
            )

        assert result.isError is False
        assert result.structuredContent == {"accepted": True}
        assert len(registry.calls) == 1

    asyncio.run(scenario())


def test_dynamic_manager_maps_public_calls_and_preserves_provider_result() -> None:
    descriptor = _descriptor("click", public_name="relay_cua_driver_click")
    registry = _Registry({"cua-driver.click": descriptor})
    manager = DynamicToolManager(
        registry=registry,
        timeout_seconds=4.0,
        device_id="device-1",
    )

    result = asyncio.run(
        manager.call_tool(
            descriptor.public_name,
            {"value": "target"},
            convert_result=True,
        )
    )

    assert isinstance(result, CallToolResult)
    assert result.model_dump(mode="json", by_alias=True, exclude_none=True) == {
        "content": [
            {"type": "text", "text": "clicked"},
            {
                "type": "image",
                "data": "aGVsbG8=",
                "mimeType": "image/png",
            },
        ],
        "structuredContent": {"accepted": True},
        "isError": False,
    }
    assert len(registry.calls) == 1
    device_id, message, timeout = registry.calls[0]
    assert device_id == "device-1"
    assert message.tool_name == "cua-driver.click"
    assert message.arguments == {"value": "target"}
    assert timeout == 4.0


def test_dynamic_manager_preserves_provider_error_results() -> None:
    descriptor = _descriptor("click", public_name="relay_cua_driver_click")
    registry = _Registry({"cua-driver.click": descriptor})
    registry.result = ProviderToolResult(
        content=[
            ProviderTextContent(type="text", text="provider rejected the action")
        ],
        structured_content={"reason": "policy"},
        is_error=True,
    )
    manager = DynamicToolManager(registry=registry, timeout_seconds=1.0)

    result = asyncio.run(
        manager.call_tool(descriptor.public_name, {"value": "target"})
    )

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert result.structuredContent == {"reason": "policy"}
    assert result.content[0].text == "provider rejected the action"  # type: ignore[union-attr]


def test_dynamic_manager_rejects_unknown_and_invalid_calls_before_provider() -> None:
    descriptor = _descriptor("click", public_name="relay_cua_driver_click")
    registry = _Registry({"cua-driver.click": descriptor})
    manager = DynamicToolManager(registry=registry, timeout_seconds=1.0)

    with pytest.raises(ToolError, match="unknown or unselected"):
        asyncio.run(manager.call_tool("relay_cua_driver_missing", {}))
    with pytest.raises(ToolError, match="invalid tool arguments"):
        asyncio.run(manager.call_tool(descriptor.public_name, {"unexpected": True}))

    assert registry.calls == []


def test_dynamic_manager_reflects_new_descriptors_after_agent_update() -> None:
    first = _descriptor("first", public_name="relay_cua_driver_first")
    second = _descriptor("second", public_name="relay_cua_driver_second")
    registry = _Registry({"cua-driver.first": first})
    manager = DynamicToolManager(registry=registry, timeout_seconds=1.0)

    assert [tool.name for tool in manager.list_tools()] == [first.public_name]
    registry.descriptors = {"cua-driver.second": second}
    assert [tool.name for tool in manager.list_tools()] == [second.public_name]

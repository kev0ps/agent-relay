from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

import pytest
from mcp.client import Client

from agent_relay.catalog import (
    CatalogError,
    CatalogService,
    local_provider_registrations,
)
from agent_relay.json_bounds import JsonValue
from agent_relay.mcp_facade import create_mcp_facade
from agent_relay.output_models import ProviderTextContent, ProviderToolResult
from agent_relay.provider_tools import ProviderToolDescriptor

CUA_NAMES = (
    "list_apps",
    "list_windows",
    "get_window_state",
    "verify_state",
    "launch_app",
    "kill_app",
    "bring_to_front",
    "set_window_frame",
    "invoke_menu",
    "debug_window_info",
    "click",
    "double_click",
    "right_click",
    "drag",
    "type_text",
    "press_key",
    "hotkey",
    "set_value",
    "scroll",
    "clipboard_read",
    "clipboard_write",
    "get_screen_size",
    "get_desktop_state",
    "get_cursor_position",
    "move_cursor",
    "set_agent_cursor_enabled",
    "set_agent_cursor_motion",
    "get_agent_cursor_state",
    "set_agent_cursor_theme",
    "check_permissions",
    "health_report",
    "get_config",
    "set_config",
    "get_accessibility_tree",
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
    "get_recording_state",
    "replay_trajectory",
    "install_ffmpeg",
    "start_session",
    "escalate_session",
    "get_session_state",
    "end_session",
    "check_for_update",
)


class FakeCuaProvider:
    def __init__(self, names: Sequence[str] = CUA_NAMES) -> None:
        self.descriptors = tuple(
            ProviderToolDescriptor(
                provider_name="cua",
                tool_name=name,
                public_name=name,
                description=f"fake CUA tool {name}",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                risk="interaction",
            )
            for name in names
        )
        self.calls: list[tuple[str, Mapping[str, JsonValue]]] = []
        self.closed = False

    async def list_tools(self) -> Sequence[ProviderToolDescriptor]:
        return self.descriptors

    async def call_tool(
        self, tool_name: str, arguments: Mapping[str, JsonValue]
    ) -> ProviderToolResult:
        self.calls.append((tool_name, arguments))
        return ProviderToolResult(
            content=[ProviderTextContent(type="text", text="provider-result")],
            structured_content={"tool": tool_name, "arguments": dict(arguments)},
        )

    async def close(self) -> None:
        self.closed = True


def discover(provider: FakeCuaProvider):
    registrations = local_provider_registrations(providers={"cua": provider})
    return asyncio.run(CatalogService(registrations).discover())


def test_all_fifty_runtime_cua_tools_are_candidates_but_not_selected() -> None:
    snapshot = discover(FakeCuaProvider())

    cua_entries = tuple(entry for entry in snapshot.entries if entry.provider_name == "cua")
    assert len(cua_entries) == 55
    assert snapshot.selected_public_names == ()
    assert {entry.public_name for entry in cua_entries} == {
        f"relay_cua_{name}" for name in CUA_NAMES
    }
    assert all(entry.status in {"disabled", "blocked"} for entry in cua_entries)
    assert snapshot.entry("relay_cua_click").status == "disabled"
    assert snapshot.entry("relay_cua_page").status == "blocked"


def test_cua_policy_classifies_risks_and_requires_explicit_selection() -> None:
    snapshot = discover(FakeCuaProvider())
    by_name = {entry.tool_name: entry for entry in snapshot.entries}

    assert by_name["list_apps"].risk == "read_only"
    assert by_name["kill_app"].risk == "destructive"
    assert by_name["set_config"].risk == "admin"
    assert by_name["page"].risk == "blocked"
    assert by_name["start_session"].status == "disabled"
    assert by_name["end_session"].status == "disabled"

    selected = snapshot.select(("relay_cua_click", "relay_cua_start_session"))
    assert selected.entry("relay_cua_click").status == "enabled"
    assert selected.entry("relay_cua_start_session").status == "enabled"
    assert selected.selected_public_names == (
        "relay_cua_click",
        "relay_cua_start_session",
    )

    with pytest.raises(CatalogError, match="blocked Agent tool"):
        snapshot.validate_allowlist(("relay_cua_page",))


def test_cua_inventory_keeps_unknown_tools_visible_but_blocked() -> None:
    provider = FakeCuaProvider((*CUA_NAMES, "future_tool"))
    snapshot = discover(provider)

    cua_entries = tuple(entry for entry in snapshot.entries if entry.provider_name == "cua")
    assert len(cua_entries) == 56
    assert snapshot.entry("relay_cua_future_tool").status == "blocked"
    assert snapshot.entry("relay_cua_future_tool").risk == "blocked"
    assert snapshot.selected_public_names == ()


def test_cua_provider_result_preserves_generic_mcp_content_and_arguments() -> None:
    provider = FakeCuaProvider()

    result = asyncio.run(provider.call_tool("click", {"element": "opaque"}))

    assert result.structured_content == {
        "tool": "click",
        "arguments": {"element": "opaque"},
    }
    assert isinstance(result.content[0], ProviderTextContent)
    assert result.content[0].text == "provider-result"
    assert provider.calls == [("click", {"element": "opaque"})]


def test_unknown_cua_tool_cannot_be_selected_or_routed() -> None:
    provider = FakeCuaProvider((*CUA_NAMES, "future_tool"))
    snapshot = discover(provider)

    with pytest.raises(CatalogError, match="blocked Agent tool"):
        snapshot.select(("relay_cua_future_tool",)).validate_allowlist(
            ("relay_cua_future_tool",)
        )


def test_configured_cua_driver_is_used_for_ephemeral_catalog_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConfiguredCapability:
        started = False
        closed = False

        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def start(self) -> None:
            self.started = True

        async def list_tools(self) -> Sequence[ProviderToolDescriptor]:
            return (
                ProviderToolDescriptor(
                    provider_name="cua",
                    tool_name="click",
                    public_name="click",
                    description="configured click",
                    input_schema={
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    risk="interaction",
                ),
            )

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        "agent_relay.capabilities.computer.ComputerCapability",
        ConfiguredCapability,
    )
    registrations = local_provider_registrations(
        env={
            "RELAY_AGENT_COMPUTER_ALLOWED_APP_NAME": "Fixture",
            "RELAY_AGENT_COMPUTER_ALLOWED_WINDOW_TITLE": "Fixture Window",
        }
    )
    capability = next(
        registration.client._capability  # type: ignore[union-attr]
        for registration in registrations
        if registration.name == "cua"
    )
    snapshot = asyncio.run(CatalogService(registrations).discover())

    assert snapshot.providers[-1].status == "available"
    assert snapshot.entry("relay_cua_click").status == "disabled"
    assert capability.started is True
    assert capability.closed is True


def test_selected_cua_descriptor_is_published_and_unselected_is_rejected() -> None:
    provider = FakeCuaProvider()
    snapshot = discover(provider).select(("relay_cua_click",))
    descriptors = {
        f"{descriptor.provider_name}.{descriptor.tool_name}": descriptor
        for descriptor in snapshot.selected_descriptors
    }

    class Registry:
        calls: list[str] = []

        @property
        def announced_descriptors(self) -> dict[str, ProviderToolDescriptor]:
            return descriptors

        async def invoke(
            self,
            device_id: str | None,
            message: object,
            timeout_seconds: float,
        ) -> ProviderToolResult:
            del device_id, timeout_seconds
            self.calls.append(message.tool_name)  # type: ignore[attr-defined]
            return await provider.call_tool("click", {})

    registry = Registry()

    async def scenario() -> None:
        mcp = create_mcp_facade(
            registry=registry,  # type: ignore[arg-type]
            timeout_seconds=1.0, only_announced=True
        )
        async with Client(mcp) as session:
            tools = (await session.list_tools()).tools
            assert [tool.name for tool in tools] == [
                "relay_device_status",
                "relay_cua_click",
            ]
            result = await session.call_tool("relay_cua_click", {})
            assert result.is_error is False
            unselected = await session.call_tool("relay_cua_type_text", {})
            assert unselected.is_error is True

    asyncio.run(scenario())
    assert registry.calls == ["cua.click"]

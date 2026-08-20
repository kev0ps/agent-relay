from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest
from mcp.types import CallToolResult

from scripts.e2e import scenarios


class Result:
    def __init__(
        self,
        *,
        structured: dict[str, object] | None = None,
        is_error: bool = False,
    ) -> None:
        self.structured_content = structured or {}
        self.is_error = is_error
        self.content: list[object] = []


def runtime(tmp_path: Path, *, browser_launch_path: str = "") -> scenarios.RuntimeConfig:
    return scenarios.RuntimeConfig(
        mcp_url="http://127.0.0.1:8000/mcp",
        control_token="control-token",
        device_id="native-e2e-agent",
        run_id="browser-run",
        fixture_url="http://127.0.0.1:1/",
        fixtures_root=str(tmp_path),
        browser_launch_path=browser_launch_path,
    )


def test_runtime_config_is_frozen_and_string_only() -> None:
    assert dataclasses.is_dataclass(scenarios.RuntimeConfig)
    assert getattr(scenarios.RuntimeConfig, "__dataclass_params__").frozen is True
    assert all(
        field.type in (str, "str")
        for field in dataclasses.fields(scenarios.RuntimeConfig)
    )


def test_expected_tool_inventories_are_closed_and_unique() -> None:
    for inventory in (
        scenarios.EXPECTED_MCP_TOOLS,
        scenarios.CORE_MCP_TOOLS,
        scenarios.CUA_MCP_TOOLS,
    ):
        assert isinstance(inventory, tuple)
        assert len(inventory) == len(set(inventory))
        assert all(name.startswith("relay_") for name in inventory)


def test_browser_window_wait_reads_mcp2_structured_content() -> None:
    class MCP2Client:
        async def call(self, _tool_name: str, _arguments: dict[str, object]) -> CallToolResult:
            return CallToolResult(
                content=[],
                structuredContent={
                    "windows": [
                        {
                            "window_id": 77,
                            "is_on_screen": True,
                            "bounds": {"width": 800, "height": 600},
                        }
                    ],
                },
            )

    assert asyncio.run(
        scenarios._wait_for_cua_browser_window(MCP2Client(), 1234, None)
    ) == 77


def test_browser_window_identity_wait_retries_transient_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    class MCPClient:
        async def call(self, tool_name: str, arguments: dict[str, object]) -> CallToolResult:
            nonlocal attempts
            assert tool_name == "relay_cua_list_windows"
            assert arguments == {"pid": 1234}
            attempts += 1
            if attempts == 1:
                return CallToolResult(content=[], structuredContent={"windows": []})
            return CallToolResult(
                content=[],
                structuredContent={
                    "windows": [
                        {
                            "window_id": 77,
                            "pid": 1234,
                            "app_name": "Google Chrome",
                            "title": "Relay CUA Fixture",
                            "is_on_screen": True,
                            "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
                        }
                    ],
                },
            )

    monkeypatch.setattr(scenarios.time, "sleep", lambda _seconds: None)
    assert asyncio.run(
        scenarios._wait_for_cua_browser_identity(MCPClient(), pid=1234, phase=None)
    ) == 77
    assert attempts == 2


def test_browser_binding_wait_retries_transient_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    class MCPClient:
        async def call(self, tool_name: str, arguments: dict[str, object]) -> CallToolResult:
            nonlocal attempts
            assert tool_name == "relay_cua_get_browser_state"
            assert arguments == {
                "pid": 1234,
                "window_id": 77,
                "session": "session-id",
            }
            attempts += 1
            tabs = [] if attempts < 3 else [{"tab_id": "tab-id", "active": True}]
            return CallToolResult(
                content=[],
                structuredContent={
                    "pid": 1234,
                    "window_id": 77,
                    "target_id": "target-id",
                    "tabs": tabs,
                },
            )

    monkeypatch.setattr(scenarios.time, "sleep", lambda _seconds: None)
    assert asyncio.run(
        scenarios._wait_for_cua_browser_binding(
            MCPClient(),
            pid=1234,
            window_id=77,
            session="session-id",
            phase=None,
        )
    ) == ("target-id", "tab-id")
    assert attempts == 3


def test_core_scenario_uses_harness_workspace_pwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, str]] = []

    class FakeSession:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def list_tools(self) -> tuple[str, ...]:
            return scenarios.CORE_MCP_TOOLS

        async def call(self, _tool_name: str, _arguments: dict[str, object]) -> object:
            return object()

    monkeypatch.setattr(scenarios._mcp, "MCPClientSession", FakeSession)
    monkeypatch.setattr(scenarios._oracles, "validate_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scenarios._oracles, "validate_ping", lambda _result: None)

    def validate_terminal(
        _result: object,
        *,
        command_id: str,
        expected: str,
    ) -> None:
        observed.append((command_id, expected))

    monkeypatch.setattr(scenarios._oracles, "validate_terminal", validate_terminal)

    scenarios.run_core_scenario(runtime(Path("/tmp/fixtures")), expected_pwd="/tmp/workspace")

    assert observed == [
        ("pwd", "/tmp/workspace"),
        ("git_branch", "relay-e2e-marker"),
    ]


class BrowserSession:
    calls: list[tuple[str, dict[str, object]]]
    browser_value: str = ""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.calls = []

    async def __aenter__(self) -> "BrowserSession":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def list_tools(self) -> tuple[str, ...]:
        return scenarios.CUA_MCP_TOOLS

    async def call(self, tool_name: str, arguments: dict[str, object]) -> Result:
        self.calls.append((tool_name, dict(arguments)))
        if tool_name == "relay_cua_launch_app":
            return Result(structured={"pid": 5000})
        if tool_name == "relay_cua_list_windows":
            return Result(
                structured={
                    "windows": [
                        {
                            "window_id": 77,
                            "pid": 5000,
                            "is_on_screen": True,
                            "bounds": {"width": 800, "height": 600},
                        }
                    ]
                }
            )
        if tool_name == "relay_cua_browser_prepare":
            return Result(structured={"status": "ok", "prepared": True, "prepared_pid": 5001})
        if tool_name == "relay_cua_get_browser_state":
            if "target_id" not in arguments:
                return Result(
                    structured={
                        "pid": 5001,
                        "window_id": 77,
                        "target_id": "target-1",
                        "tabs": [{"tab_id": "tab-1", "active": True}],
                    }
                )
            return Result(
                structured={
                    "target_id": "target-1",
                    "url": "http://127.0.0.1:1/",
                    "text": "Relay CUA Fixture applied" if self.browser_value else "Relay CUA Fixture",
                    "tabs": [{"tab_id": "tab-1", "active": True}],
                    "elements": [
                        {
                            "ref": "p1:0",
                            "role": "textbox",
                            "name": "Name",
                            "value": self.browser_value,
                            "editable": True,
                            "enabled": True,
                            "clickable": False,
                        },
                        {
                            "ref": "p1:1",
                            "role": "button",
                            "name": "Apply",
                            "value": "",
                            "editable": False,
                            "enabled": True,
                            "clickable": True,
                        },
                    ],
                }
            )
        if tool_name == "relay_cua_browser_type":
            self.browser_value = str(arguments["text"])
        return Result()


def test_browser_scenario_uses_one_shared_fixture_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = BrowserSession("ignored")
    executable = tmp_path / "chrome"
    executable.write_bytes(b"chrome")

    monkeypatch.setattr(scenarios._mcp, "MCPClientSession", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(scenarios._oracles, "validate_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scenarios._oracles, "validate_cua_browser_launch", lambda _result: 5000)
    monkeypatch.setattr(scenarios._oracles, "validate_cua_list_windows", lambda _result, **_kwargs: (5001, 77))
    monkeypatch.setattr(scenarios._oracles, "validate_cua_browser_binding", lambda _result, **_kwargs: ("target-1", "tab-1"))
    monkeypatch.setattr(scenarios._oracles, "validate_cua_browser_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scenarios._oracles, "validate_cua_browser_controls", lambda *_args, **_kwargs: ("p1:0", "p1:1"))
    monkeypatch.setattr(scenarios._oracles, "validate_cua_browser_success", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scenarios._oracles, "poll_cua_event", lambda *_args, **_kwargs: b"event")
    monkeypatch.setattr(scenarios.time, "sleep", lambda _seconds: None)

    scenarios.run_browser_scenario(
        runtime(tmp_path, browser_launch_path=str(executable)),
        "relay-value",
        expected_capabilities=("cua.browser_click",),
    )

    names = [name for name, _arguments in session.calls]
    assert names == [
        "relay_device_status",
        "relay_cua_launch_app",
        "relay_cua_start_session",
        "relay_cua_list_windows",
        "relay_cua_browser_prepare",
        "relay_cua_list_windows",
        "relay_cua_get_browser_state",
        "relay_cua_browser_navigate",
        "relay_cua_get_browser_state",
        "relay_cua_browser_type",
        "relay_cua_browser_click",
        "relay_cua_get_browser_state",
        "relay_cua_end_session",
        "relay_cua_kill_app",
        "relay_cua_kill_app",
    ]
    launch = dict(session.calls[1][1])
    assert launch["launch_path"] == str(executable)
    arguments = launch["additional_arguments"]
    assert isinstance(arguments, list)
    assert "http://127.0.0.1:1/" in arguments


def test_browser_scenario_rejects_unexpected_tool_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class UnexpectedSession(BrowserSession):
        async def list_tools(self) -> tuple[str, ...]:
            return (*scenarios.CUA_MCP_TOOLS, "relay_unexpected_tool")

    session = UnexpectedSession()
    monkeypatch.setattr(scenarios._mcp, "MCPClientSession", lambda *_args, **_kwargs: session)

    with pytest.raises(ValueError, match="unexpected MCP tools"):
        scenarios.run_browser_scenario(runtime(tmp_path), "relay-value")

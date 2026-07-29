"""Contract tests for the portable MCP oracle helpers.

These tests pin the validation contract that the portable kernel
applies to ``CallToolResult`` payloads. They are derived from the
invariants in ``AGENTS.md`` (closed authority surface, strict typing,
fail-closed dispatch).

The portable oracle helpers are intentionally re-derived from those invariants
rather than copied from a platform-specific harness.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

E2E_DIR = Path(__file__).resolve().parent / "e2e"


def _load(rel_filename: str, dotted: str) -> ModuleType:
    cached = sys.modules.get(dotted)
    if cached is not None:
        return cached
    target = E2E_DIR / rel_filename
    spec = importlib.util.spec_from_file_location(dotted, target)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {dotted} from {target}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = module
    spec.loader.exec_module(module)
    return module


def _oracles() -> ModuleType:
    return _load("oracles.py", "tests.e2e.oracles")


# --- Helpers ---------------------------------------------------------------


def _make_call_tool_result(
    structured: dict[str, Any] | None,
    *,
    is_error: bool = False,
    extra_fields: dict[str, Any] | None = None,
) -> Any:
    """Build a minimal ``CallToolResult``-like object for unit tests.

    The portable oracle only relies on three attributes:
    ``structuredContent``, ``isError``, and ``model_extra``. Tests below
    stay close to that surface so they can run without importing the
    MCP SDK.
    """
    if extra_fields is None:
        extra_fields = {}
    return type(
        "FakeResult",
        (),
        {
            "structuredContent": structured,
            "isError": is_error,
            "model_extra": extra_fields or None,
        },
    )()


def _good_status_payload(device_id: str, *, connected: bool) -> dict[str, Any]:
    """Produce a status payload that satisfies every AGENTS.md invariant."""
    payload: dict[str, Any] = {
        "device_id": device_id,
        "connected": connected,
        "capabilities": (
            sorted(
                [
                    "system.ping",
                    "terminal.exec",
                    "browser.list_tabs",
                    "browser.navigate",
                    "browser.read_page",
                    "browser.fill",
                    "browser.click",
                    "computer.capture",
                    "computer.click",
                    "computer.type",
                ]
            )
            if connected
            else []
        ),
        "invocation_state": "idle",
        "progress": None,
        "heartbeat_age_seconds": 0.5 if connected else None,
    }
    return payload


# --- Tests -----------------------------------------------------------------


def test_oracles_module_exposes_validate_status() -> None:
    """``tests/e2e/oracles.py`` exposes ``validate_status``."""
    oracles = _oracles()
    assert hasattr(oracles, "validate_status"), (
        "tests/e2e/oracles.py must define validate_status(result, device_id, connected)"
    )


def test_validate_status_accepts_a_well_formed_connected_payload() -> None:
    """A status payload that matches every invariant passes silently."""
    oracles = _oracles()
    result = _make_call_tool_result(_good_status_payload("test-device", connected=True))
    # Must not raise.
    oracles.validate_status(result, device_id="test-device", connected=True)


def test_validate_status_accepts_a_well_formed_disconnected_payload() -> None:
    """When the agent is disconnected, capabilities and heartbeat are absent."""
    oracles = _oracles()
    result = _make_call_tool_result(_good_status_payload("test-device", connected=False))
    oracles.validate_status(result, device_id="test-device", connected=False)


def test_validate_status_accepts_a_narrow_core_capability_inventory() -> None:
    """A native core harness may advertise only its enabled capabilities."""
    oracles = _oracles()
    payload = _good_status_payload("test-device", connected=True)
    payload["capabilities"] = ["system.ping", "terminal.exec"]
    result = _make_call_tool_result(payload)
    oracles.validate_status(
        result,
        device_id="test-device",
        connected=True,
        expected_capabilities=("system.ping", "terminal.exec"),
    )


def test_validate_status_rejects_extra_top_level_keys() -> None:
    """Closed authority surface: any unknown field fails closed."""
    oracles = _oracles()
    payload = _good_status_payload("test-device", connected=True)
    payload["rogue_field"] = "value"
    result = _make_call_tool_result(payload, extra_fields={"rogue_field": "value"})
    with pytest.raises(ValueError):
        oracles.validate_status(result, device_id="test-device", connected=True)


def test_validate_status_rejects_wrong_device_id() -> None:
    """The kernel refuses payloads whose device_id does not match the harness."""
    oracles = _oracles()
    result = _make_call_tool_result(_good_status_payload("other-device", connected=True))
    with pytest.raises(ValueError):
        oracles.validate_status(result, device_id="test-device", connected=True)


def test_validate_status_rejects_is_error_true() -> None:
    """An error result is not a valid status."""
    oracles = _oracles()
    result = _make_call_tool_result(_good_status_payload("test-device", connected=True), is_error=True)
    with pytest.raises(ValueError):
        oracles.validate_status(result, device_id="test-device", connected=True)


def test_validate_status_rejects_non_string_device_id() -> None:
    """Strict typing: ``device_id`` must be a string, never an int or None."""
    oracles = _oracles()
    payload = _good_status_payload("test-device", connected=True)
    payload["device_id"] = 42  # type: ignore[assignment]
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_status(result, device_id="test-device", connected=True)


def test_validate_status_rejects_non_dict_structured_content() -> None:
    """Strict typing: ``structuredContent`` must be a dict, never None or a list."""
    oracles = _oracles()
    result = _make_call_tool_result(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        oracles.validate_status(result, device_id="test-device", connected=True)


def test_validate_status_rejects_missing_heartbeat_when_connected() -> None:
    """When the agent is connected, a finite heartbeat age is required."""
    oracles = _oracles()
    payload = _good_status_payload("test-device", connected=True)
    payload["heartbeat_age_seconds"] = None
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_status(result, device_id="test-device", connected=True)


def test_validate_status_rejects_negative_heartbeat() -> None:
    """A heartbeat age must be finite and non-negative."""
    oracles = _oracles()
    payload = _good_status_payload("test-device", connected=True)
    payload["heartbeat_age_seconds"] = -1.0
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_status(result, device_id="test-device", connected=True)


def test_validate_status_rejects_non_idle_invocation_state() -> None:
    """Status is only meaningful when the agent is idle."""
    oracles = _oracles()
    payload = _good_status_payload("test-device", connected=True)
    payload["invocation_state"] = "running"
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_status(result, device_id="test-device", connected=True)


def test_validate_status_rejects_capabilities_with_non_string_items() -> None:
    """Every capability entry must be a string."""
    oracles = _oracles()
    payload = _good_status_payload("test-device", connected=True)
    payload["capabilities"] = [1, 2, 3]  # type: ignore[list-item]
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_status(result, device_id="test-device", connected=True)


# --- Ping oracle ------------------------------------------------------------


def test_oracles_module_exposes_validate_ping() -> None:
    oracles = _oracles()
    assert hasattr(oracles, "validate_ping")


def test_validate_ping_accepts_pong_true() -> None:
    oracles = _oracles()
    result = _make_call_tool_result({"pong": True})
    oracles.validate_ping(result)


def test_validate_ping_rejects_pong_false() -> None:
    oracles = _oracles()
    result = _make_call_tool_result({"pong": False})
    with pytest.raises(ValueError):
        oracles.validate_ping(result)


def test_validate_ping_rejects_extra_keys() -> None:
    oracles = _oracles()
    result = _make_call_tool_result({"pong": True, "extra": 1})
    with pytest.raises(ValueError):
        oracles.validate_ping(result)


def test_validate_ping_rejects_non_dict_payload() -> None:
    oracles = _oracles()
    result = _make_call_tool_result(None)
    with pytest.raises(ValueError):
        oracles.validate_ping(result)


# --- Terminal oracle --------------------------------------------------------


def test_oracles_module_exposes_validate_terminal() -> None:
    oracles = _oracles()
    assert hasattr(oracles, "validate_terminal")


def _good_terminal_payload(command_id: str, expected: str) -> dict[str, Any]:
    return {
        "command_id": command_id,
        "stdout": f"{expected}\n",
        "stderr": "",
        "exit_code": 0,
        "timed_out": False,
        "stdout_truncated": False,
        "stderr_truncated": False,
    }


def test_validate_terminal_accepts_well_formed_marker_payload() -> None:
    oracles = _oracles()
    result = _make_call_tool_result(_good_terminal_payload("git-branch", "main"))
    oracles.validate_terminal(result, command_id="git-branch", expected="main")


def test_validate_terminal_rejects_non_zero_exit_code() -> None:
    oracles = _oracles()
    payload = _good_terminal_payload("git-branch", "main")
    payload["exit_code"] = 1
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_terminal(result, command_id="git-branch", expected="main")


def test_validate_terminal_rejects_timed_out_true() -> None:
    oracles = _oracles()
    payload = _good_terminal_payload("git-branch", "main")
    payload["timed_out"] = True
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_terminal(result, command_id="git-branch", expected="main")


def test_validate_terminal_rejects_stderr_non_empty() -> None:
    oracles = _oracles()
    payload = _good_terminal_payload("git-branch", "main")
    payload["stderr"] = "warning: something\n"
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_terminal(result, command_id="git-branch", expected="main")


def test_validate_terminal_rejects_command_id_mismatch() -> None:
    oracles = _oracles()
    result = _make_call_tool_result(_good_terminal_payload("git-branch", "main"))
    with pytest.raises(ValueError):
        oracles.validate_terminal(result, command_id="pwd", expected="main")


def test_validate_terminal_rejects_stdout_missing_trailing_newline() -> None:
    oracles = _oracles()
    payload = _good_terminal_payload("git-branch", "main")
    payload["stdout"] = "main"  # missing \n
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_terminal(result, command_id="git-branch", expected="main")


def test_validate_terminal_rejects_extra_keys() -> None:
    oracles = _oracles()
    payload = _good_terminal_payload("git-branch", "main")
    payload["leak"] = True
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_terminal(result, command_id="git-branch", expected="main")


# --- List tabs oracle -------------------------------------------------------


def test_oracles_module_exposes_validate_list_tabs() -> None:
    oracles = _oracles()
    assert hasattr(oracles, "validate_list_tabs")


def _good_list_tabs_payload() -> dict[str, Any]:
    return {
        "tabs": [
            {"tab_id": "tab-1", "title": "Agent Relay", "url": "about:blank"},
        ],
    }


def test_validate_list_tabs_returns_tab_id_for_one_blank_tab() -> None:
    oracles = _oracles()
    result = _make_call_tool_result(_good_list_tabs_payload())
    assert oracles.validate_list_tabs(result) == "tab-1"


def test_validate_list_tabs_rejects_non_blank_url() -> None:
    oracles = _oracles()
    payload = _good_list_tabs_payload()
    payload["tabs"][0]["url"] = "https://example.com/"
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_list_tabs(result)


def test_validate_list_tabs_rejects_zero_tabs() -> None:
    oracles = _oracles()
    payload: dict[str, Any] = {"tabs": []}
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_list_tabs(result)


def test_validate_list_tabs_rejects_tab_with_extra_keys() -> None:
    oracles = _oracles()
    payload = _good_list_tabs_payload()
    payload["tabs"][0]["screenshot"] = "raw-bytes"
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_list_tabs(result)


def test_validate_list_tabs_rejects_oversized_tab_id() -> None:
    oracles = _oracles()
    payload = _good_list_tabs_payload()
    payload["tabs"][0]["tab_id"] = "x" * 200
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_list_tabs(result)


# --- Action oracle ----------------------------------------------------------


def test_oracles_module_exposes_validate_action() -> None:
    oracles = _oracles()
    assert hasattr(oracles, "validate_action")


def _good_action_payload(
    *, tab_id: str, element_id: str | None, fixture_url: str, fixture_title: str
) -> dict[str, Any]:
    return {
        "tab_id": tab_id,
        "element_id": element_id,
        "url": fixture_url,
        "title": fixture_title,
        "success": True,
    }


def test_validate_action_accepts_well_formed_navigate_payload() -> None:
    oracles = _oracles()
    payload = _good_action_payload(
        tab_id="tab-1",
        element_id=None,
        fixture_url="http://127.0.0.1:8899/",
        fixture_title="Relay Browser Fixture",
    )
    result = _make_call_tool_result(payload)
    oracles.validate_action(
        result,
        tool_name="relay_browser_navigate",
        tab_id="tab-1",
        element_id=None,
        fixture_url="http://127.0.0.1:8899/",
        fixture_title="Relay Browser Fixture",
    )


def test_validate_action_accepts_well_formed_click_payload() -> None:
    oracles = _oracles()
    payload = _good_action_payload(
        tab_id="tab-1",
        element_id="btn-1",
        fixture_url="http://127.0.0.1:8899/",
        fixture_title="Relay Browser Fixture",
    )
    result = _make_call_tool_result(payload)
    oracles.validate_action(
        result,
        tool_name="relay_browser_click",
        tab_id="tab-1",
        element_id="btn-1",
        fixture_url="http://127.0.0.1:8899/",
        fixture_title="Relay Browser Fixture",
    )


def test_validate_action_rejects_success_false() -> None:
    oracles = _oracles()
    payload = _good_action_payload(
        tab_id="tab-1",
        element_id=None,
        fixture_url="http://127.0.0.1:8899/",
        fixture_title="Relay Browser Fixture",
    )
    payload["success"] = False
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_action(
            result,
            tool_name="relay_browser_navigate",
            tab_id="tab-1",
            element_id=None,
            fixture_url="http://127.0.0.1:8899/",
            fixture_title="Relay Browser Fixture",
        )


def test_validate_action_rejects_tab_id_mismatch() -> None:
    oracles = _oracles()
    payload = _good_action_payload(
        tab_id="tab-other",
        element_id=None,
        fixture_url="http://127.0.0.1:8899/",
        fixture_title="Relay Browser Fixture",
    )
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_action(
            result,
            tool_name="relay_browser_navigate",
            tab_id="tab-1",
            element_id=None,
            fixture_url="http://127.0.0.1:8899/",
            fixture_title="Relay Browser Fixture",
        )


def test_validate_action_rejects_url_outside_origin_allowlist() -> None:
    oracles = _oracles()
    payload = _good_action_payload(
        tab_id="tab-1",
        element_id=None,
        fixture_url="http://127.0.0.1:8899/",
        fixture_title="Relay Browser Fixture",
    )
    payload["url"] = "https://attacker.example/"
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_action(
            result,
            tool_name="relay_browser_navigate",
            tab_id="tab-1",
            element_id=None,
            fixture_url="http://127.0.0.1:8899/",
            fixture_title="Relay Browser Fixture",
        )


# --- Computer capture oracle ------------------------------------------------


def test_oracles_module_exposes_validate_computer_capture() -> None:
    oracles = _oracles()
    assert hasattr(oracles, "validate_computer_capture")


def _good_computer_capture_payload() -> dict[str, Any]:
    return {
        "app": "relay-desktop-fixture",
        "window_title": "Relay Desktop Fixture",
        "generation": "gen-001",
        "elements": [
            {
                "element_id": "field-1",
                "role": "textbox",
                "name": "Name",
                "value": "",
                "enabled": True,
            },
            {
                "element_id": "btn-1",
                "role": "button",
                "name": "Apply",
                "value": None,
                "enabled": True,
            },
        ],
    }


def test_validate_computer_capture_returns_field_and_button_ids() -> None:
    oracles = _oracles()
    result = _make_call_tool_result(_good_computer_capture_payload())
    field_id, button_id = oracles.validate_computer_capture(result)
    assert field_id == "field-1"
    assert button_id == "btn-1"


def test_validate_computer_capture_rejects_wrong_app_name() -> None:
    oracles = _oracles()
    payload = _good_computer_capture_payload()
    payload["app"] = "some-other-app"
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_computer_capture(result)


def test_validate_computer_capture_rejects_wrong_window_title() -> None:
    oracles = _oracles()
    payload = _good_computer_capture_payload()
    payload["window_title"] = "Personal Chrome"
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_computer_capture(result)


def test_validate_computer_capture_rejects_duplicate_element_ids() -> None:
    oracles = _oracles()
    payload = _good_computer_capture_payload()
    payload["elements"].append(dict(payload["elements"][0]))
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_computer_capture(result)


def test_validate_computer_capture_rejects_decoy_control() -> None:
    """A control whose name matches a known decoy is rejected.

    Decoys prevent the agent from being manipulated into acting on
    sensitive-looking UI that does not exist in the fixture.
    """
    oracles = _oracles()
    payload = _good_computer_capture_payload()
    payload["elements"].append(
        {
            "element_id": "decoy-1",
            "role": "button",
            "name": "vault password",
            "value": None,
            "enabled": True,
        }
    )
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_computer_capture(result)


def test_validate_computer_capture_rejects_missing_field_control() -> None:
    oracles = _oracles()
    payload = _good_computer_capture_payload()
    # Remove the textbox element; only the button remains.
    payload["elements"] = [payload["elements"][1]]
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_computer_capture(result)


def test_validate_computer_capture_rejects_missing_button_control() -> None:
    oracles = _oracles()
    payload = _good_computer_capture_payload()
    payload["elements"] = [payload["elements"][0]]
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_computer_capture(result)


def test_validate_computer_capture_rejects_oversized_generation() -> None:
    oracles = _oracles()
    payload = _good_computer_capture_payload()
    payload["generation"] = "g" * 200
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_computer_capture(result)


def test_validate_computer_capture_accepts_diagnostic_phase() -> None:
    """The optional ``diagnostic_phase`` list receives failure markers."""
    oracles = _oracles()
    result = _make_call_tool_result(_good_computer_capture_payload())
    phase: list[str] = []
    oracles.validate_computer_capture(result, diagnostic_phase=phase)
    # On success, phase may be empty or end with a success marker.
    # The contract is that the harness receives any markers that
    # happened; we only assert that the call did not raise.


def test_validate_computer_capture_marks_failure_in_diagnostic_phase() -> None:
    oracles = _oracles()
    bad_result = _make_call_tool_result({"rogue": True})
    phase: list[str] = []
    with pytest.raises(ValueError):
        oracles.validate_computer_capture(bad_result, diagnostic_phase=phase)
    assert phase, "diagnostic_phase must be populated on failure"


# --- Computer action oracle -------------------------------------------------


def test_oracles_module_exposes_validate_computer_action() -> None:
    oracles = _oracles()
    assert hasattr(oracles, "validate_computer_action")


def _good_computer_action_payload(generation: str, element_id: str) -> dict[str, Any]:
    return {
        "success": True,
        "generation": generation,
        "element_id": element_id,
    }


def test_validate_computer_action_accepts_well_formed_payload() -> None:
    oracles = _oracles()
    result = _make_call_tool_result(_good_computer_action_payload("gen-001", "field-1"))
    oracles.validate_computer_action(
        result,
        tool_name="relay_computer_click",
        generation="gen-001",
        element_id="field-1",
    )


def test_validate_computer_action_rejects_success_false() -> None:
    oracles = _oracles()
    payload = _good_computer_action_payload("gen-001", "field-1")
    payload["success"] = False
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_computer_action(
            result,
            tool_name="relay_computer_click",
            generation="gen-001",
            element_id="field-1",
        )


def test_validate_computer_action_rejects_generation_mismatch() -> None:
    oracles = _oracles()
    result = _make_call_tool_result(_good_computer_action_payload("gen-001", "field-1"))
    with pytest.raises(ValueError):
        oracles.validate_computer_action(
            result,
            tool_name="relay_computer_click",
            generation="gen-other",
            element_id="field-1",
        )


def test_validate_computer_action_rejects_element_id_mismatch() -> None:
    oracles = _oracles()
    result = _make_call_tool_result(_good_computer_action_payload("gen-001", "field-1"))
    with pytest.raises(ValueError):
        oracles.validate_computer_action(
            result,
            tool_name="relay_computer_click",
            generation="gen-001",
            element_id="field-other",
        )


def test_validate_computer_action_rejects_extra_keys() -> None:
    oracles = _oracles()
    payload = _good_computer_action_payload("gen-001", "field-1")
    payload["screenshot"] = "raw-bytes"
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_computer_action(
            result,
            tool_name="relay_computer_click",
            generation="gen-001",
            element_id="field-1",
        )


# --- Read page oracle -------------------------------------------------------


def test_oracles_module_exposes_validate_read_page() -> None:
    oracles = _oracles()
    assert hasattr(oracles, "validate_read_page")


def _good_read_page_payload(tab_id: str) -> dict[str, Any]:
    return {
        "tab_id": tab_id,
        "url": "http://127.0.0.1:8899/",
        "title": "Relay Browser Fixture",
        "text": "Relay Browser Fixture\nName\nSubmit\n",
        "elements": [
            {
                "element_id": "field-1",
                "role": "textbox",
                "name": "Name",
                "value": "",
                "editable": True,
                "enabled": True,
            },
            {
                "element_id": "btn-1",
                "role": "button",
                "name": "Submit",
                "value": None,
                "editable": False,
                "enabled": True,
            },
        ],
    }


def test_validate_read_page_returns_textbox_and_button_ids() -> None:
    oracles = _oracles()
    result = _make_call_tool_result(_good_read_page_payload("tab-1"))
    textbox_id, button_id = oracles.validate_read_page(
        result, tab_id="tab-1", fixture_url="http://127.0.0.1:8899/", fixture_title="Relay Browser Fixture"
    )
    assert textbox_id == "field-1"
    assert button_id == "btn-1"


def test_validate_read_page_rejects_tab_id_mismatch() -> None:
    oracles = _oracles()
    result = _make_call_tool_result(_good_read_page_payload("tab-1"))
    with pytest.raises(ValueError):
        oracles.validate_read_page(
            result, tab_id="tab-other", fixture_url="http://127.0.0.1:8899/", fixture_title="Relay Browser Fixture"
        )


def test_validate_read_page_rejects_text_missing_required_markers() -> None:
    oracles = _oracles()
    payload = _good_read_page_payload("tab-1")
    payload["text"] = "Some other text"
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_read_page(
            result, tab_id="tab-1", fixture_url="http://127.0.0.1:8899/", fixture_title="Relay Browser Fixture"
        )


def test_validate_read_page_rejects_oversized_text() -> None:
    oracles = _oracles()
    payload = _good_read_page_payload("tab-1")
    payload["text"] = "Name Submit " + ("x" * 5000)
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_read_page(
            result, tab_id="tab-1", fixture_url="http://127.0.0.1:8899/", fixture_title="Relay Browser Fixture"
        )


def test_validate_read_page_rejects_missing_textbox() -> None:
    oracles = _oracles()
    payload = _good_read_page_payload("tab-1")
    payload["elements"] = [payload["elements"][1]]  # only button
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_read_page(
            result, tab_id="tab-1", fixture_url="http://127.0.0.1:8899/", fixture_title="Relay Browser Fixture"
        )


def test_validate_read_page_rejects_element_with_wrong_shape() -> None:
    oracles = _oracles()
    payload = _good_read_page_payload("tab-1")
    payload["elements"][0]["bogus"] = True
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_read_page(
            result, tab_id="tab-1", fixture_url="http://127.0.0.1:8899/", fixture_title="Relay Browser Fixture"
        )


# --- Fixture event oracles --------------------------------------------------


def test_oracles_module_exposes_fixture_event_helpers() -> None:
    oracles = _oracles()
    for name in (
        "validate_fixture_event",
        "validate_computer_event",
        "assert_no_fixture_event",
        "poll_fixture_event",
    ):
        assert hasattr(oracles, name), f"missing {name}"


def _write_event(tmp_path, payload: dict[str, str]):
    """Write a single-event JSONL file in the portable format."""
    import json

    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
    path: Path = tmp_path / "event.jsonl"
    path.write_bytes(encoded)
    return path


def test_validate_fixture_event_accepts_well_formed_submitted_event(tmp_path) -> None:
    oracles = _oracles()
    path = _write_event(tmp_path, {"run_id": "run-1", "event": "submitted", "value": "abc"})
    oracles.validate_fixture_event(path, run_id="run-1", value="abc")


def test_validate_fixture_event_rejects_run_id_mismatch(tmp_path) -> None:
    oracles = _oracles()
    path = _write_event(tmp_path, {"run_id": "run-1", "event": "submitted", "value": "abc"})
    with pytest.raises(ValueError):
        oracles.validate_fixture_event(path, run_id="run-other", value="abc")


def test_validate_fixture_event_rejects_value_mismatch(tmp_path) -> None:
    oracles = _oracles()
    path = _write_event(tmp_path, {"run_id": "run-1", "event": "submitted", "value": "abc"})
    with pytest.raises(ValueError):
        oracles.validate_fixture_event(path, run_id="run-1", value="different")


def test_validate_fixture_event_rejects_extra_keys(tmp_path) -> None:
    oracles = _oracles()
    path = _write_event(tmp_path, {"run_id": "run-1", "event": "submitted", "value": "abc", "leak": "x"})
    with pytest.raises(ValueError):
        oracles.validate_fixture_event(path, run_id="run-1", value="abc")


def test_validate_fixture_event_rejects_missing_file(tmp_path) -> None:
    oracles = _oracles()
    with pytest.raises(ValueError):
        oracles.validate_fixture_event(tmp_path / "absent.jsonl", run_id="run-1", value="abc")


def test_validate_fixture_event_rejects_non_jsonl_content(tmp_path) -> None:
    oracles = _oracles()

    path: Path = tmp_path / "event.jsonl"
    path.write_bytes(b"not a json object\n")
    with pytest.raises(ValueError):
        oracles.validate_fixture_event(path, run_id="run-1", value="abc")


def test_validate_computer_event_accepts_well_formed_applied_event(tmp_path) -> None:
    oracles = _oracles()
    path = _write_event(tmp_path, {"run_id": "run-1", "event": "applied", "value": "abc"})
    oracles.validate_computer_event(path, run_id="run-1", value="abc")


def test_validate_computer_event_rejects_wrong_event_kind(tmp_path) -> None:
    oracles = _oracles()
    # Submitted is for browser fixture, applied is for computer fixture.
    path = _write_event(tmp_path, {"run_id": "run-1", "event": "submitted", "value": "abc"})
    with pytest.raises(ValueError):
        oracles.validate_computer_event(path, run_id="run-1", value="abc")


def test_assert_no_fixture_event_passes_when_absent(tmp_path) -> None:
    oracles = _oracles()
    oracles.assert_no_fixture_event(tmp_path / "absent.jsonl")


def test_assert_no_fixture_event_raises_when_present(tmp_path) -> None:
    oracles = _oracles()
    _write_event(tmp_path, {"run_id": "run-1", "event": "submitted", "value": "abc"})
    with pytest.raises(ValueError):
        oracles.assert_no_fixture_event(tmp_path / "event.jsonl")


def test_poll_fixture_event_returns_when_event_arrives(tmp_path) -> None:
    oracles = _oracles()
    import threading
    import time

    path: Path = tmp_path / "event.jsonl"

    def write_later() -> None:
        time.sleep(0.05)
        _write_event(tmp_path, {"run_id": "run-1", "event": "submitted", "value": "abc"})

    threading.Thread(target=write_later, daemon=True).start()
    oracles.poll_fixture_event(path, run_id="run-1", value="abc", timeout=2.0)


def test_poll_fixture_event_times_out_when_event_never_appears(tmp_path) -> None:
    oracles = _oracles()

    path: Path = tmp_path / "event.jsonl"
    with pytest.raises(TimeoutError):
        oracles.poll_fixture_event(path, run_id="run-1", value="abc", timeout=0.2)


def test_poll_fixture_event_rejects_invalid_event_when_present(tmp_path) -> None:
    oracles = _oracles()
    # An invalid event (wrong run_id) appears immediately.
    _write_event(tmp_path, {"run_id": "run-other", "event": "submitted", "value": "abc"})
    with pytest.raises(ValueError):
        oracles.poll_fixture_event(tmp_path / "event.jsonl", run_id="run-1", value="abc", timeout=0.5)


def test_validate_action_rejects_extra_keys() -> None:
    oracles = _oracles()
    payload = _good_action_payload(
        tab_id="tab-1",
        element_id=None,
        fixture_url="http://127.0.0.1:8899/",
        fixture_title="Relay Browser Fixture",
    )
    payload["screenshot"] = "raw-bytes"
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_action(
            result,
            tool_name="relay_browser_navigate",
            tab_id="tab-1",
            element_id=None,
            fixture_url="http://127.0.0.1:8899/",
            fixture_title="Relay Browser Fixture",
        )


def test_validate_status_accepts_connected_status_payload_shape() -> None:
    """A connected capability list in sorted wire order validates."""
    oracles = _oracles()
    payload = {
        "device_id": "test-device",
        "connected": True,
        "capabilities": sorted(
            [
                "system.ping",
                "terminal.exec",
                "browser.list_tabs",
                "browser.navigate",
                "browser.read_page",
                "browser.fill",
                "browser.click",
                "computer.capture",
                "computer.click",
                "computer.type",
            ]
        ),
        "invocation_state": "idle",
        "progress": None,
        "heartbeat_age_seconds": 0.0,
    }
    result = _make_call_tool_result(payload)
    oracles.validate_status(result, device_id="test-device", connected=True)


def test_validate_status_rejects_progress_set() -> None:
    """Status reports no in-flight progress; a non-null progress is invalid."""
    oracles = _oracles()
    payload = _good_status_payload("test-device", connected=True)
    payload["progress"] = {"phase": "compute"}
    result = _make_call_tool_result(payload)
    with pytest.raises(ValueError):
        oracles.validate_status(result, device_id="test-device", connected=True)
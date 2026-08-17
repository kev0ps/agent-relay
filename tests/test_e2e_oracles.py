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
                    "cua.click",
                    "cua.get_window_state",
                    "cua.list_windows",
                    "cua.type_text",
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


def test_validate_status_accepts_initial_unenrolled_offline_payload() -> None:
    """Before enrollment the dynamic registry reports no device identity."""
    oracles = _oracles()
    payload = _good_status_payload("test-device", connected=False)
    payload["device_id"] = None
    result = _make_call_tool_result(payload)
    oracles.validate_status(
        result,
        device_id=None,
        connected=False,
        allow_unenrolled=True,
    )

    with pytest.raises(ValueError):
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


# --- Generic CUA provider oracles -------------------------------------------


def _good_cua_windows_payload() -> dict[str, Any]:
    return {
        "windows": [
            {
                "window_id": 77,
                "pid": 1234,
                "app_name": "relay-desktop-fixture",
                "title": "Relay Desktop Fixture",
                "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
                "is_on_screen": True,
            }
        ]
    }


def test_validate_cua_list_windows_returns_pid_and_window_id() -> None:
    pid, window_id = _oracles().validate_cua_list_windows(
        _make_call_tool_result(_good_cua_windows_payload()),
        expected_app="relay-desktop-fixture",
        expected_window_title="Relay Desktop Fixture",
    )
    assert (pid, window_id) == (1234, 77)


def test_validate_cua_list_windows_rejects_wrong_window_identity() -> None:
    payload = _good_cua_windows_payload()
    payload["windows"][0]["title"] = "Other Window"
    with pytest.raises(ValueError):
        _oracles().validate_cua_list_windows(
            _make_call_tool_result(payload),
            expected_app="relay-desktop-fixture",
            expected_window_title="Relay Desktop Fixture",
        )


def _good_cua_state_payload() -> dict[str, Any]:
    return {
        "window_id": 77,
        "pid": 1234,
        "snapshot_id": "snapshot-001",
        "elements": [
            {
                "element_index": 0,
                "element_token": "token-field-001",
                "role": "entry",
                "label": "Name",
                "enabled": True,
            },
            {
                "element_index": 1,
                "element_token": "token-button-001",
                "role": "push button",
                "label": "Apply",
                "enabled": True,
            },
        ],
    }


def test_validate_cua_window_state_returns_bounded_tokens() -> None:
    snapshot, field, button = _oracles().validate_cua_window_state(
        _make_call_tool_result(_good_cua_state_payload()),
        expected_pid=1234,
        window_id=77,
    )
    assert (snapshot, field, button) == (
        "snapshot-001",
        "token-field-001",
        "token-button-001",
    )


def test_validate_cua_window_state_rejects_duplicate_indices() -> None:
    payload = _good_cua_state_payload()
    payload["elements"][1]["element_index"] = 0
    with pytest.raises(ValueError):
        _oracles().validate_cua_window_state(
            _make_call_tool_result(payload), expected_pid=1234, window_id=77
        )


def test_validate_cua_window_state_rejects_oversized_tokens() -> None:
    payload = _good_cua_state_payload()
    payload["elements"][0]["element_token"] = "x" * 300
    with pytest.raises(ValueError):
        _oracles().validate_cua_window_state(
            _make_call_tool_result(payload), expected_pid=1234, window_id=77
        )


def test_validate_cua_action_accepts_bounded_result() -> None:
    _oracles().validate_cua_action(
        _make_call_tool_result(
            {"path": "native_input", "verified": False, "effect": "unverifiable"}
        ),
        tool_name="relay_cua_click",
    )


def test_validate_cua_browser_controls_returns_opaque_refs() -> None:
    payload = {
        "target_id": "target-1",
        "tabs": [{"tab_id": "tab-1", "active": True}],
        "url": "http://127.0.0.1:8898/",
        "title": "Relay Desktop Fixture",
        "text": "Relay Desktop Fixture Name Apply idle",
        "elements": [
            {
                "ref": "p1:0",
                "role": "textbox",
                "name": "Name",
                "value": "",
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
    assert _oracles().validate_cua_browser_controls(
        _make_call_tool_result(payload),
        expected_url="http://127.0.0.1:8898/",
    ) == ("p1:0", "p1:1")


def test_validate_cua_browser_controls_rejects_native_handles() -> None:
    payload = {
        "target_id": "target-1",
        "tabs": [{"tab_id": "tab-1", "active": True}],
        "url": "http://127.0.0.1:8898/",
        "text": "Relay Desktop Fixture Name Apply",
        "elements": [
            {
                "ref": "p1:0",
                "role": "textbox",
                "name": "Name",
                "editable": True,
                "enabled": True,
                "clickable": False,
                "element_token": "native-handle",
            },
            {
                "ref": "p1:1",
                "role": "button",
                "name": "Apply",
                "editable": False,
                "enabled": True,
                "clickable": True,
            },
        ],
    }
    with pytest.raises(ValueError):
        _oracles().validate_cua_browser_controls(
            _make_call_tool_result(payload),
            expected_url="http://127.0.0.1:8898/",
        )


def test_validate_cua_browser_launch_accepts_cua_019_shape() -> None:
    assert _oracles().validate_cua_browser_launch(
        _make_call_tool_result(
            {
                "pid": 4321,
                "name": "chromium",
                "active": False,
                "windows": [],
                "bundle_id": None,
            }
        )
    ) == 4321


def test_validate_cua_browser_launch_rejects_missing_native_name() -> None:
    with pytest.raises(ValueError):
        _oracles().validate_cua_browser_launch(
            _make_call_tool_result({"pid": 4321, "active": False, "windows": []})
        )


def test_validate_cua_action_rejects_raw_screenshot_or_token() -> None:
    payload = {
        "path": "native_input",
        "verified": False,
        "effect": "unverifiable",
        "screenshot": "raw-bytes",
        "element_token": "token-field-001",
    }
    with pytest.raises(ValueError):
        _oracles().validate_cua_action(
            _make_call_tool_result(payload), tool_name="relay_cua_click"
        )


# --- Fixture event oracles --------------------------------------------------


def test_oracles_module_exposes_cua_event_helpers() -> None:
    oracles = _oracles()
    for name in (
        "validate_cua_event",
        "assert_no_cua_event",
        "poll_cua_event",
    ):
        assert hasattr(oracles, name), f"missing {name}"


def _write_event(tmp_path, payload: dict[str, str]):
    """Write a single-event JSONL file in the portable format."""
    import json

    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
    path: Path = tmp_path / "event.jsonl"
    path.write_bytes(encoded)
    return path


def test_validate_cua_event_accepts_well_formed_applied_event(tmp_path) -> None:
    oracles = _oracles()
    path = _write_event(tmp_path, {"run_id": "run-1", "event": "applied", "value": "abc"})
    oracles.validate_cua_event(path, run_id="run-1", value="abc")


def test_validate_cua_event_rejects_wrong_event_kind(tmp_path) -> None:
    oracles = _oracles()
    path = _write_event(tmp_path, {"run_id": "run-1", "event": "unexpected", "value": "abc"})
    with pytest.raises(ValueError):
        oracles.validate_cua_event(path, run_id="run-1", value="abc")


def test_assert_no_cua_event_passes_when_absent(tmp_path) -> None:
    oracles = _oracles()
    oracles.assert_no_cua_event(tmp_path / "absent.jsonl")


def test_assert_no_cua_event_raises_when_present(tmp_path) -> None:
    oracles = _oracles()
    _write_event(tmp_path, {"run_id": "run-1", "event": "applied", "value": "abc"})
    with pytest.raises(ValueError):
        oracles.assert_no_cua_event(tmp_path / "event.jsonl")


def test_poll_cua_event_returns_when_event_arrives(tmp_path) -> None:
    oracles = _oracles()
    import threading
    import time

    path: Path = tmp_path / "event.jsonl"

    def write_later() -> None:
        time.sleep(0.05)
        _write_event(tmp_path, {"run_id": "run-1", "event": "applied", "value": "abc"})

    threading.Thread(target=write_later, daemon=True).start()
    oracles.poll_cua_event(path, run_id="run-1", value="abc", timeout=2.0)


def test_poll_cua_event_times_out_when_event_never_appears(tmp_path) -> None:
    oracles = _oracles()

    path: Path = tmp_path / "event.jsonl"
    with pytest.raises(TimeoutError):
        oracles.poll_cua_event(path, run_id="run-1", value="abc", timeout=0.2)


def test_poll_cua_event_rejects_invalid_event_when_present(tmp_path) -> None:
    oracles = _oracles()
    # An invalid event (wrong run_id) appears immediately.
    _write_event(tmp_path, {"run_id": "run-other", "event": "applied", "value": "abc"})
    with pytest.raises(ValueError):
        oracles.poll_cua_event(tmp_path / "event.jsonl", run_id="run-1", value="abc", timeout=0.5)


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
                "cua.click",
                "cua.get_window_state",
                "cua.list_windows",
                "cua.type_text",
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

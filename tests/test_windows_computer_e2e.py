from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

from agent_relay.capabilities.computer import (
    _WINDOWS_TOOL_ADDITIONAL_PROPERTIES,
    _WINDOWS_TOOL_FIELDS,
    _WINDOWS_TOOL_REQUIRED,
    _process_creation_options,
    validate_windows_health,
)

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
FIXTURE = ROOT / "scripts/windows_computer_use_fixture.ps1"


def _tool(name: str) -> dict[str, object]:
    fields = _WINDOWS_TOOL_FIELDS[name]
    required = _WINDOWS_TOOL_REQUIRED[name]
    return {
        "name": name,
        "inputSchema": {
            "type": "object",
            "properties": {field: {} for field in fields},
            "required": sorted(required),
            "additionalProperties": _WINDOWS_TOOL_ADDITIONAL_PROPERTIES.get(name, False),
        },
    }


def test_windows_driver_contract_requires_ui_automation_tools_but_hides_extras() -> None:
    from agent_relay.capabilities.computer import ComputerCapability

    result = {
        "tools": [_tool(name) for name in _WINDOWS_TOOL_FIELDS]
        + [{"name": "launch_app", "inputSchema": {"type": "object"}}],
    }

    ComputerCapability._validate_tools(result, windows=True)


def test_windows_driver_schema_allows_new_optional_uia_fields() -> None:
    from agent_relay.capabilities.computer import ComputerCapability

    result = {"tools": [_tool(name) for name in _WINDOWS_TOOL_FIELDS]}
    tools = cast(list[dict[str, object]], result["tools"])
    schema = cast(dict[str, object], tools[0]["inputSchema"])
    properties = cast(dict[str, object], schema["properties"])
    properties["future_option"] = {"type": "string"}

    ComputerCapability._validate_tools(result, windows=True)


def test_windows_driver_health_contract_requires_interactive_uia_session() -> None:
    validate_windows_health(
        {
            "schema_version": "1",
            "platform": "win32",
            "overall": "ok",
            "checks": [
                {"name": "binary_version", "status": "pass", "message": "ok"},
                {"name": "platform_supported", "status": "pass", "message": "ok"},
                {"name": "session_active", "status": "pass", "message": "ok"},
                {"name": "ax_capability", "status": "pass", "message": "ok"},
            ],
        }
    )


def test_windows_process_options_do_not_use_posix_process_groups() -> None:
    options = _process_creation_options(windows=True)
    assert "start_new_session" not in options
    assert options["creationflags"] == getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def test_windows_ci_has_a_native_cua_job_and_bounded_oracle() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    job = workflow.split("  e2e-windows-cua:", 1)[1].split(
        "\n  e2e-windows-browser:", 1
    )[0]

    assert "name: Native Windows CUA end-to-end" in job
    assert "runs-on: windows-2025" in job
    assert "Install pinned cua-driver" in job
    assert "scripts/windows_computer_e2e.py" in job
    assert "windows-cua-evidence" in job
    assert "computer-events.jsonl" in job
    assert "event.event -cne \"applied\"" in job
    assert "Session 0" in job
    assert "docker run" not in job.lower()
    assert "docker.sock" not in job.lower()
    assert "--privileged" not in job.lower()


def test_windows_cua_fixture_emits_the_shared_computer_oracle_contract() -> None:
    fixture = FIXTURE.read_text(encoding="utf-8")
    assert "RunId" in fixture
    assert "run_id = $RunId" in fixture
    assert 'event = "applied"' in fixture
    assert "eventpath" in fixture.casefold()
    assert '$submit.Text = "Apply"' in fixture
    assert '$submit.AccessibleName = "Apply"' in fixture


def test_windows_cua_passes_fixture_identity_to_portable_scenario() -> None:
    source = (ROOT / "scripts" / "windows_computer_e2e.py").read_text(
        encoding="utf-8"
    )

    assert "expected_computer_app=COMPUTER_APP_NAME" in source
    assert "expected_computer_window_title=COMPUTER_WINDOW_TITLE" in source


def test_windows_cua_contract_test_is_json_serializable() -> None:
    payload = {"tools": [_tool(name) for name in _WINDOWS_TOOL_FIELDS]}
    assert json.loads(json.dumps(payload)) == payload

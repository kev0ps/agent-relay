from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agent_relay.capabilities.computer import (
    _process_creation_options,
    validate_windows_health,
)
from agent_relay.catalog import CUA_REFERENCE_TOOL_NAMES

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
FIXTURE = ROOT / "scripts/windows_computer_use_fixture.ps1"
INSTALLER = ROOT / "scripts/install_windows_cua_driver.ps1"


def test_windows_cua_reference_inventory_is_generic_and_bounded() -> None:
    assert len(CUA_REFERENCE_TOOL_NAMES) == 50
    payload = {
        "tools": [
            {
                "name": name,
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            }
            for name in CUA_REFERENCE_TOOL_NAMES
        ]
    }
    assert json.loads(json.dumps(payload)) == payload


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
    assert "windows-cua-evidence" in job
    assert "docker run" not in job.lower()
    assert "docker.sock" not in job.lower()
    assert "--privileged" not in job.lower()
    assert "scripts/install_windows_cua_driver.ps1" in job
    assert "scripts/probe_cua_driver.py --platform windows" in job
    assert "--profile windows-cua" in job


def test_windows_cua_installer_verifies_the_pinned_source_hash() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")

    assert "1760f253d3c4d76618a8c97a04f2c100ffc491ac" in installer
    assert 'DriverVersion = "0.12.6"' in installer
    assert "85227ad5400240ccdcd8be18024ad871d1382d9e0b7f66dcce778e0ae4427f73" in installer
    assert "Get-FileHash" in installer
    assert "SHA256 mismatch" in installer


def test_windows_cua_fixture_is_not_a_relay_dispatch_layer() -> None:
    fixture = FIXTURE.read_text(encoding="utf-8")
    assert "RunId" in fixture
    assert "eventpath" in fixture.casefold()
    assert '$input.AccessibleName = "Name"' in fixture

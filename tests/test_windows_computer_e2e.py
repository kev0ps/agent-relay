from __future__ import annotations

import subprocess
from pathlib import Path

from agent_relay.capabilities.computer import (
    _process_creation_options,
    validate_windows_health,
)

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
FIXTURE = ROOT / "scripts/windows_computer_use_fixture.ps1"


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
    job = workflow.split("  e2e-windows-cua:", 1)[1]
    assert "name: Windows CUA end-to-end" in job
    assert "runs-on: windows-2025" in job
    assert "windows-cua-evidence" in job
    assert "docker run" not in job.lower()
    assert "docker.sock" not in job.lower()
    assert "--privileged" not in job.lower()
    assert "Verify automatic CUA driver resolution" in workflow
    assert "scripts/probe_cua_driver.py --platform windows" in job
    assert "--profile windows-cua" in job


def test_windows_cua_fixture_is_not_a_relay_dispatch_layer() -> None:
    fixture = FIXTURE.read_text(encoding="utf-8")
    assert "RunId" in fixture
    assert "eventpath" in fixture.casefold()
    assert '$input.AccessibleName = "Name"' in fixture

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "linux_computer_e2e.py"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
DESKTOP_FIXTURE = ROOT / "tests" / "fixtures" / "desktop_app.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("linux_computer_e2e", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_linux_cua_chromium_command_is_accessible_and_loopback() -> None:
    harness = _load_harness()
    command = harness.chromium_command(
        Path("/opt/chromium/chromium"),
        Path("/tmp/relay-cua-profile"),
        "http://127.0.0.1:23456/",
    )
    assert "--force-renderer-accessibility" in command
    assert "--no-sandbox" in command
    assert "--disable-dev-shm-usage" in command
    assert "--user-data-dir=/tmp/relay-cua-profile" in command
    assert all("0.0.0.0" not in item for item in command)


def test_linux_cua_uses_production_configuration_and_fixture() -> None:
    harness = _load_harness()
    source = SCRIPT.read_text(encoding="utf-8")
    assert harness.COMPUTER_APP_NAME == "relay-desktop-fixture"
    assert harness.COMPUTER_WINDOW_TITLE == "Relay Desktop Fixture"
    assert 'DESKTOP_FIXTURE = ROOT / "tests" / "fixtures" / "desktop_app.py"' in source
    for key in (
        "RELAY_AGENT_COMPUTER_DRIVER_PATH",
        "RELAY_AGENT_COMPUTER_ALLOWED_APP_NAME",
        "RELAY_AGENT_COMPUTER_ALLOWED_WINDOW_TITLE",
    ):
        assert key in source
    assert "relay_computer_capture" in (ROOT / "tests" / "e2e" / "scenarios.py").read_text(encoding="utf-8")
    assert "spikes/computer-use-xvfb" not in source
    assert "expected_computer_app=COMPUTER_APP_NAME" in source
    assert "expected_computer_window_title=COMPUTER_WINDOW_TITLE" in source


def test_linux_cua_ci_job_invokes_public_runtime_gate() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "  e2e-linux-cua:" in workflow
    job = workflow.split("  e2e-linux-cua:", 1)[1].split(
        "\n  e2e-windows-native:", 1
    )[0]
    assert "name: Native Linux CUA end-to-end" in job
    assert "runs-on: ubuntu-24.04" in job
    assert "uv sync --locked --extra browser --extra computer" in job
    assert "xvfb" in job.lower()
    assert "at-spi" in job.lower()
    assert "uv run --frozen python scripts/linux_computer_e2e.py" in job
    assert "computer-events.jsonl" in job
    assert "docker" not in job.lower()
    assert "spikes/computer-use-xvfb" not in job
    assert "if: always()" in job


def test_linux_cua_failure_evidence_does_not_require_fixture_event() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    job = workflow.split("  e2e-linux-cua:", 1)[1].split(
        "\n  e2e-windows-native:", 1
    )[0]

    success_branch = job.index(
        "if test \"$output\" = 'Linux CUA smoke scenario passed.'; then"
    )
    required_event = job.index('test -f "$evidence_dir/computer-events.jsonl"')

    assert success_branch < required_event
    assert 'if test -e "$evidence_dir/computer-events.jsonl"; then' in job
    assert 'test ! -e "$path" && test ! -L "$path"' in job

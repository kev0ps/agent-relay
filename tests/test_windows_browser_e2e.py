from __future__ import annotations

import importlib.util
import inspect
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "windows_browser_e2e.py"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
FIXTURE = ROOT / "tests" / "fixtures" / "browser_app.py"
SCENARIOS = ROOT / "tests" / "e2e" / "scenarios.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("windows_browser_e2e", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_windows_browser_is_launched_by_agent_persistent_context() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "RELAY_AGENT_BROWSER_USER_DATA_DIR" in source
    assert "RELAY_AGENT_BROWSER_HEADLESS" in source
    assert "RELAY_AGENT_BROWSER_ALLOWED_ORIGINS" in source
    assert "chromium_command" not in source
    assert "remote-debugging" not in source
    assert "browser_cdp" not in source
    assert "screenshot" not in source.lower()


def test_fixture_command_uses_the_bounded_loopback_fixture() -> None:
    harness = _load_harness()
    command = harness.fixture_command(23457, "windows-browser-test")

    assert command == [
        sys.executable,
        str(FIXTURE),
        "--run-id",
        "windows-browser-test",
        "--host",
        "127.0.0.1",
        "--port",
        "23457",
    ]


def test_browser_capabilities_cover_requested_tools_and_exclude_computer_use() -> None:
    harness = _load_harness()

    assert harness.BROWSER_CAPABILITIES == (
        "browser.back",
        "browser.click",
        "browser.fill",
        "browser.list_tabs",
        "browser.navigate",
        "browser.scroll",
        "browser.snapshot",
        "browser.type",
        "system.ping",
        "terminal.exec",
    )
    assert not any(item.startswith("computer.") for item in harness.BROWSER_CAPABILITIES)


@pytest.mark.skipif(os.name == "nt", reason="tests the non-Windows guard")
def test_run_scenario_requires_windows() -> None:
    harness = _load_harness()

    with pytest.raises(harness.WindowsBrowserE2EError, match="requires Windows"):
        harness.run_scenario()


def test_shared_browser_scenario_accepts_expected_capabilities() -> None:
    spec = importlib.util.spec_from_file_location("e2e_scenarios", SCENARIOS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["e2e_scenarios"] = module
    spec.loader.exec_module(module)

    signature = inspect.signature(module.run_browser_scenario)
    assert "expected_capabilities" in signature.parameters
    assert signature.parameters["expected_capabilities"].default is None


def test_browser_scenario_contains_negative_origin_stale_and_navigation_gates() -> None:
    source = SCENARIOS.read_text(encoding="utf-8")

    assert '"disallowed-origin"' in source
    assert '"locator-refresh"' in source
    assert '"relay_browser_back"' in source
    assert '"scroll-down"' in source
    assert "disallowed Browser origin was not safely rejected" in source
    assert "structured locator" in source


def test_windows_browser_waits_for_diagnostics_before_removing_temp_root() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    temporary_cleanup = source.index(
        'lifecycle.add_cleanup(temporary.cleanup, label="temporary-directory")'
    )
    diagnostics_cleanup = source.index(
        'lifecycle.add_cleanup(lifecycle.wait_for_diagnostics, label="diagnostics")'
    )
    diagnostic_report = source.index('label="diagnostic-classification"')
    streams_cleanup = source.index('label="diagnostic-streams"')
    job_cleanup = source.index('label="windows-job"')

    assert temporary_cleanup < diagnostic_report < diagnostics_cleanup < streams_cleanup < job_cleanup


def test_windows_browser_ci_job_is_persistent_context_and_bounded() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "  e2e-windows-browser:" in workflow
    job = workflow.split("  e2e-windows-browser:", 1)[1]

    assert "name: Native Windows Browser end-to-end" in job
    assert "needs: python" in job
    assert "runs-on: windows-2025" in job
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in job
    assert "uv sync --locked --extra browser" in job
    assert "uv run --frozen playwright install chromium" in job
    assert "uv run --frozen python scripts/windows_browser_e2e.py" in job
    assert "success.json" in job
    assert "browser-events.jsonl" in job
    assert "screenshot.png" not in job
    assert "remote-debugging" not in job
    assert "docker" not in job.lower()
    assert "computer" not in job.lower()
    assert "headed" not in job.lower()
    assert "id: validate-windows-browser-evidence" in job
    assert "Browser screenshot" not in job
    assert "if: always()" in job


def test_windows_browser_failure_evidence_does_not_require_success_payloads() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    job = workflow.split("  e2e-windows-browser:", 1)[1]

    success_branch = job.index(
        'if ($output -eq "Windows Browser smoke scenario passed.")'
    )
    required_event = job.index(
        'if (-not (Test-Path -LiteralPath $eventPath -PathType Leaf))'
    )

    assert success_branch < required_event
    assert 'if (Test-Path -LiteralPath $eventPath -PathType Leaf)' in job
    assert "screenshot.png" not in job

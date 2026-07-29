from __future__ import annotations

import asyncio
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
    spec.loader.exec_module(module)
    return module


def test_browser_command_is_headless_and_loopback_only(tmp_path: Path) -> None:
    harness = _load_harness()
    executable = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    command = harness.chromium_command(executable, 23456, tmp_path / "profile")

    assert command[0] == str(executable)
    assert "--headless=new" in command
    assert "--no-first-run" in command
    assert "--no-default-browser-check" in command
    assert "--remote-debugging-address=127.0.0.1" in command
    assert "--remote-debugging-port=23456" in command
    assert f"--user-data-dir={tmp_path / 'profile'}" in command
    assert "--remote-debugging-address=0.0.0.0" not in command
    assert "--disable-web-security" not in command


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


def test_browser_capabilities_exclude_computer_use() -> None:
    harness = _load_harness()

    assert harness.BROWSER_CAPABILITIES == (
        "browser.click",
        "browser.fill",
        "browser.list_tabs",
        "browser.navigate",
        "browser.read_page",
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


def test_browser_scenario_contains_negative_origin_and_stale_element_gates() -> None:
    source = SCENARIOS.read_text(encoding="utf-8")

    assert '"disallowed-origin"' in source
    assert '"stale-element"' in source
    assert "disallowed Browser origin was not safely rejected" in source
    assert "stale Browser element was not safely rejected" in source


def test_screenshot_validation_requires_bounded_nonzero_dimensions() -> None:
    harness = _load_harness()
    signature = b"\x89PNG\r\n\x1a\n"

    valid = signature + b"\x00\x00\x00\x0dIHDR" + (1280).to_bytes(4, "big") + (800).to_bytes(4, "big")
    assert harness.validate_screenshot_png(valid) == (1280, 800)

    zero_width = signature + b"\x00\x00\x00\x0dIHDR" + (0).to_bytes(4, "big") + (800).to_bytes(4, "big")
    with pytest.raises(harness.WindowsBrowserE2EError, match="dimensions"):
        harness.validate_screenshot_png(zero_width)

    oversized = signature + b"\x00\x00\x00\x0dIHDR" + (4097).to_bytes(4, "big") + (800).to_bytes(4, "big")
    with pytest.raises(harness.WindowsBrowserE2EError, match="dimensions"):
        harness.validate_screenshot_png(oversized)


def test_screenshot_capture_has_a_global_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _load_harness()

    async def never_returns(_ws_url: str) -> bytes:
        await asyncio.sleep(60)
        return b""

    monkeypatch.setattr(harness, "_fixture_page_socket", lambda *_: "ws://127.0.0.1/ws")
    monkeypatch.setattr(harness, "_capture_png", never_returns)
    monkeypatch.setattr(harness, "CDP_SCREENSHOT_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(harness.WindowsBrowserE2EError, match="timed out"):
        harness.capture_screenshot(
            "http://127.0.0.1:9222",
            "http://127.0.0.1:8000/",
            None,
        )


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


def test_windows_browser_ci_job_is_headless_and_bounded() -> None:
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
    assert "screenshot.png" in job
    assert "docker" not in job.lower()
    assert "computer" not in job.lower()
    assert "headed" not in job.lower()
    assert "remote-debugging-address=0.0.0.0" not in job
    assert "id: validate-windows-browser-evidence" in job
    assert "Browser screenshot dimensions are invalid" in job
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
    required_screenshot = job.index(
        'if (-not (Test-Path -LiteralPath $screenshotPath -PathType Leaf))'
    )

    assert success_branch < required_event
    assert success_branch < required_screenshot
    assert 'if (Test-Path -LiteralPath $eventPath -PathType Leaf)' in job
    assert 'if (Test-Path -LiteralPath $screenshotPath -PathType Leaf)' in job

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "linux_browser_e2e.py"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
FIXTURE = ROOT / "tests" / "fixtures" / "browser_app.py"
SCENARIOS = ROOT / "tests" / "e2e" / "scenarios.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("linux_browser_e2e", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_linux_browser_command_is_headless_and_loopback_only(tmp_path: Path) -> None:
    harness = _load_harness()
    executable = Path("/opt/chromium/chromium")
    command = harness.chromium_command(executable, 23456, tmp_path / "profile")

    assert command[0] == str(executable)
    assert "--headless=new" in command
    assert "--no-sandbox" in command
    assert "--disable-dev-shm-usage" in command
    assert "--no-first-run" in command
    assert "--remote-debugging-address=127.0.0.1" in command
    assert "--remote-debugging-port=23456" in command
    assert f"--user-data-dir={tmp_path / 'profile'}" in command
    assert "--remote-debugging-address=0.0.0.0" not in command
    assert "--disable-web-security" not in command


def test_linux_browser_fixture_command_uses_loopback() -> None:
    harness = _load_harness()
    assert harness.fixture_command(23457, "linux-browser-test") == [
        sys.executable,
        str(FIXTURE),
        "--run-id",
        "linux-browser-test",
        "--host",
        "127.0.0.1",
        "--port",
        "23457",
    ]


def test_linux_browser_capabilities_exclude_computer_use() -> None:
    harness = _load_harness()
    assert not any(item.startswith("computer.") for item in harness.BROWSER_CAPABILITIES)


def test_shared_browser_scenario_accepts_expected_capabilities() -> None:
    spec = importlib.util.spec_from_file_location("e2e_scenarios_linux_browser", SCENARIOS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    signature = inspect.signature(module.run_browser_scenario)
    assert "expected_capabilities" in signature.parameters


def test_linux_browser_ci_job_is_native_and_bounded() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "  e2e-linux-browser:" in workflow
    job = workflow.split("  e2e-linux-browser:", 1)[1].split(
        "\n  e2e-linux-cua:", 1
    )[0]
    assert "name: Native Linux Browser end-to-end" in job
    assert "runs-on: ubuntu-24.04" in job
    assert "uv sync --locked --extra browser" in job
    assert "uv run --frozen playwright install --with-deps chromium" in job
    assert "uv run --frozen python scripts/linux_browser_e2e.py" in job
    assert "browser-evidence" in job
    assert "browser-events.jsonl" in job
    assert "screenshot.png" in job
    assert "docker" not in job.lower()
    assert "computer" not in job.lower()
    assert "remote-debugging-address=0.0.0.0" not in job
    assert "if: always()" in job


def test_screenshot_validation_requires_bounded_nonzero_dimensions() -> None:
    harness = _load_harness()
    signature = b"\x89PNG\r\n\x1a\n"
    valid = signature + b"\x00\x00\x00\x0dIHDR" + (1280).to_bytes(4, "big") + (800).to_bytes(4, "big")
    assert harness.validate_screenshot_png(valid) == (1280, 800)
    zero_width = signature + b"\x00\x00\x00\x0dIHDR" + (0).to_bytes(4, "big") + (800).to_bytes(4, "big")
    with pytest.raises(harness.LinuxBrowserE2EError, match="dimensions"):
        harness.validate_screenshot_png(zero_width)


def test_linux_screenshot_capture_uses_one_global_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load_harness()

    async def never_returns(_ws_url: str) -> bytes:
        await asyncio.sleep(60)
        return b""

    monkeypatch.setattr(
        harness,
        "_fixture_page_socket",
        lambda *_args: "ws://127.0.0.1/ws",
    )
    monkeypatch.setattr(harness, "_capture_png", never_returns)
    monkeypatch.setattr(harness, "CDP_SCREENSHOT_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(harness.LinuxBrowserE2EError, match="timed out"):
        harness.capture_screenshot(
            "http://127.0.0.1:9222",
            "http://127.0.0.1:8000/",
            None,
        )


def test_linux_browser_failure_evidence_does_not_require_success_payloads() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    job = workflow.split("  e2e-linux-browser:", 1)[1].split(
        "\n  e2e-linux-cua:", 1
    )[0]

    success_branch = job.index(
        "if test \"$output\" = 'Linux Browser smoke scenario passed.'; then"
    )
    required_event = job.index('test -f "$evidence_dir/browser-events.jsonl"')
    required_screenshot = job.index('test -f "$evidence_dir/screenshot.png"')

    assert success_branch < required_event
    assert success_branch < required_screenshot
    assert 'if test -e "$evidence_dir/browser-events.jsonl"; then' in job
    assert 'if test -e "$evidence_dir/screenshot.png"; then' in job
    assert 'test ! -e "$path" && test ! -L "$path"' in job

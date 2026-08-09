from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

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


def test_linux_browser_is_launched_by_agent_persistent_context() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "RELAY_AGENT_BROWSER_USER_DATA_DIR" in source
    assert "RELAY_AGENT_BROWSER_HEADLESS" in source
    assert "RELAY_AGENT_BROWSER_ALLOWED_ORIGINS" in source
    assert "chromium_command" not in source
    assert "remote-debugging" not in source
    assert "browser_cdp" not in source
    assert "screenshot" not in source.lower()


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


def test_linux_browser_capabilities_cover_requested_tools_and_exclude_computer_use() -> None:
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
    assert "uses: ./.github/actions/setup-python" in job
    assert "profile: browser" in job
    assert "uv run --frozen playwright install --with-deps chromium" in job
    assert "uv run --frozen python scripts/linux_browser_e2e.py" in job
    assert "browser-evidence" in job
    assert "python scripts/validate_e2e_evidence.py" in job
    assert "--profile linux-browser" in job
    assert "screenshot.png" not in job
    assert "remote-debugging" not in job
    assert "docker" not in job.lower()
    assert "computer" not in job.lower()
    assert "if: always()" in job


def test_linux_browser_evidence_policy_is_externalized() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    job = workflow.split("  e2e-linux-browser:", 1)[1].split(
        "\n  e2e-linux-cua:", 1
    )[0]

    assert "python scripts/validate_e2e_evidence.py" in job
    assert "--profile linux-browser" in job
    assert "browser-events.jsonl" not in job
    assert "screenshot.png" not in job

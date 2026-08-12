from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
SETUP = ROOT / ".github/actions/setup-python/action.yml"
DEPENDABOT = ROOT / ".github/dependabot.yml"


def test_python_job_runs_required_quality_gates() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    python_job = workflow.split("  python:", 1)[1].split("\n  container:", 1)[0]
    assert "uv run --frozen ruff check ." in python_job
    assert 'pytest -q -m "not integration"' in python_job
    assert "pytest -q -m integration" in python_job
    assert "uv lock --check" not in python_job


def test_ci_has_only_general_cua_native_jobs() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "name: Linux CUA end-to-end" in workflow
    assert "name: Windows CUA end-to-end" in workflow
    assert ("Browser " + "end-to-end") not in workflow
    assert ("e2e-linux-" + "browser") not in workflow
    assert ("e2e-windows-" + "browser") not in workflow
    forbidden_browser_runtime = "play" + "wright"
    forbidden_manual_path = "RELAY_AGENT_" + "COMPUTER_" + "DRIVER_PATH"
    assert forbidden_browser_runtime not in workflow.casefold()
    assert forbidden_manual_path not in workflow
    assert "import cua_driver; print(cua_driver.get_binary_path())" in workflow
    assert "Verify Linux CUA browser prerequisite" in workflow
    assert "chromium --version" in workflow
    assert ("CUA_DRIVER_" + "RS_INSTALL_DIR") not in workflow


def test_cua_jobs_use_the_standard_locked_dependency_set() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    setup = SETUP.read_text(encoding="utf-8")
    for job_name in ("e2e-linux-cua", "e2e-windows-cua"):
        job = workflow.split(f"  {job_name}:", 1)[1].split("\n  e2e-", 1)[0]
        assert "scripts/probe_cua_driver.py" in job
    assert ("profile: " + "cua") not in workflow
    assert "profile:" not in setup
    assert "uv sync --locked" in setup
    assert ("AGENT_RELAY_SYNC_" + "PROFILE") not in setup
    assert "extra " + "browser" not in setup
    assert "extra " + "computer" not in setup


def test_dependabot_groups_python_and_actions_weekly_without_automerge() -> None:
    dependabot = DEPENDABOT.read_text(encoding="utf-8")
    assert "package-ecosystem: pip" in dependabot
    assert "package-ecosystem: github-actions" in dependabot
    assert dependabot.count("interval: weekly") == 2
    assert "patterns:" in dependabot
    assert "automerge" not in dependabot.casefold()

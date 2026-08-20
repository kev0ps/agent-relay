from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
SETUP = ROOT / ".github/actions/setup-python/action.yml"
DEPENDABOT = ROOT / ".github/dependabot.yml"


def test_python_job_runs_required_quality_gates_without_duplicate_lock_check() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    python_job = workflow.split("  python:", 1)[1].split("\n  container:", 1)[0]
    setup = SETUP.read_text(encoding="utf-8")
    assert "uv run --frozen ruff check ." in python_job
    assert 'pytest -q -m "not integration"' in python_job
    assert "pytest -q -m integration" in python_job
    assert "uv lock --check" not in python_job
    assert setup.count("uv lock --check") == 1
    assert "scripts/audit_dependencies.py --check" in python_job


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
    assert "Verify Linux CUA Chrome prerequisite" in workflow
    assert "Verify Windows CUA Chrome prerequisite" in workflow
    assert "command -v google-chrome" in workflow
    assert "google-chrome --version" in workflow
    assert "from scripts.e2e.chrome import find_chrome" in workflow
    assert "google-chrome-stable" not in workflow
    assert ("CUA_DRIVER_" + "RS_INSTALL_DIR") not in workflow


def test_cua_jobs_use_the_standard_locked_dependency_set() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    setup = SETUP.read_text(encoding="utf-8")
    for job_name in ("e2e-linux-cua", "e2e-windows-cua"):
        job = workflow.split(f"  {job_name}:", 1)[1].split("\n  e2e-", 1)[0]
        assert "scripts/probe_cua_driver.py" in job
        assert 'cua: "true"' in job
    assert "profile:" not in setup
    assert "uv sync --locked" in setup
    assert "--extra cua" in setup
    assert ("AGENT_RELAY_SYNC_" + "PROFILE") not in setup
    assert "extra " + "browser" not in setup
    assert "extra " + "computer" not in setup


def test_ci_uses_reproducible_pytest_module_invocation() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    pytest_lines = [line for line in workflow.splitlines() if "pytest" in line]
    assert pytest_lines
    assert all("uv run --frozen python -m pytest" in line for line in pytest_lines)
    assert "uv run --frozen pytest" not in workflow


def test_linux_cua_job_uses_preinstalled_chrome_and_bounded_budget() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    linux_cua = workflow.split("  e2e-linux-cua:", 1)[1].split(
        "\n  e2e-windows-terminal:", 1
    )[0]
    desktop_prerequisites = linux_cua.split(
        "      - name: Install Linux desktop prerequisites", 1
    )[1].split("      - name: Verify Linux CUA Chrome prerequisite", 1)[0]
    assert "timeout-minutes: 10" in linux_cua
    assert desktop_prerequisites.count("sudo apt-get update") == 1
    assert "chromium" not in desktop_prerequisites.casefold()
    assert "wget" not in desktop_prerequisites
    assert "google-chrome-stable" not in linux_cua
    assert "https://dl.google.com/linux/linux_signing_key.pub" not in linux_cua
    assert "https://dl.google.com/linux/chrome/deb/" not in linux_cua
    assert "uv run --frozen python scripts/linux_computer_e2e.py" in linux_cua


def test_windows_cua_job_uses_preinstalled_chrome_without_provisioning() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    windows_cua = workflow.split("  e2e-windows-cua:", 1)[1]

    assert "timeout-minutes: 10" in windows_cua
    assert "Verify Windows CUA Chrome prerequisite" in windows_cua
    assert "from scripts.e2e.chrome import find_chrome" in windows_cua
    assert "--version" in windows_cua
    assert "winget" not in windows_cua.casefold()
    assert "choco install" not in windows_cua.casefold()
    assert "https://dl.google.com" not in windows_cua
    assert "uv run --frozen python scripts/windows_computer_e2e.py" in windows_cua


def test_terminal_jobs_run_shared_and_os_specific_contract_tests() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    linux = workflow.split("  e2e-linux:", 1)[1].split("\n  e2e-linux-cua:", 1)[0]
    windows = workflow.split("  e2e-windows-terminal:", 1)[1].split(
        "\n  e2e-windows-cua:", 1
    )[0]

    assert "tests/test_e2e_harness.py" in linux
    assert "tests/test_linux_e2e_adapter.py" in linux
    assert "tests/test_e2e_harness.py" in windows
    assert "tests/test_windows_e2e_adapter.py" in windows


def test_cua_jobs_do_not_repeat_portable_contract_suite() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    common_tests = (
        "tests/test_chrome_e2e.py",
        "tests/test_cua_catalog.py",
        "tests/test_cua_entrypoints.py",
        "tests/test_cua_harness.py",
        "tests/test_cua_profiles.py",
        "tests/test_computer_capability.py",
        "tests/test_desktop_fixture.py",
        "tests/test_e2e_harness.py",
        "tests/test_e2e_kernel.py",
        "tests/test_e2e_mcp_client.py",
        "tests/test_e2e_oracles.py",
        "tests/test_graphical_sessions.py",
        "tests/test_linux_e2e_adapter.py",
        "tests/test_windows_e2e_adapter.py",
        "tests/test_runner.py",
    )
    for job_name in ("e2e-linux-cua", "e2e-windows-cua"):
        job = workflow.split(f"  {job_name}:", 1)[1].split("\n  e2e-", 1)[0]
        for test_path in common_tests:
            assert test_path not in job, f"{test_path} duplicated in {job_name}"


def test_dependabot_groups_python_and_actions_weekly_without_automerge() -> None:
    dependabot = DEPENDABOT.read_text(encoding="utf-8")
    assert "package-ecosystem: pip" in dependabot
    assert "package-ecosystem: github-actions" in dependabot
    assert dependabot.count("interval: weekly") == 2
    assert "patterns:" in dependabot
    assert "automerge" not in dependabot.casefold()


def test_dependabot_does_not_group_major_python_updates_after_mcp2() -> None:
    dependabot = DEPENDABOT.read_text(encoding="utf-8")
    python_config = dependabot.split("  - package-ecosystem: pip", 1)[1].split(
        "  - package-ecosystem: github-actions", 1
    )[0]

    assert 'update-types: ["patch", "minor"]' in python_config
    assert 'dependency-name: "mcp"' not in python_config

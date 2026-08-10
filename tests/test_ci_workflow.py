from __future__ import annotations

from pathlib import Path

WORKFLOW_DIR = Path(__file__).parents[1] / ".github/workflows"
WORKFLOW = WORKFLOW_DIR / "ci.yml"
PYTHON_SETUP = Path(__file__).parents[1] / ".github/actions/setup-python/action.yml"


def test_ci_reports_informational_python_coverage_without_threshold() -> None:
    workflow = WORKFLOW.read_text()
    python_job = workflow.split("  python:", 1)[1].split("\n  container:", 1)[0]

    assert "uv run --frozen ruff check ." in python_job
    assert workflow.count("uv run --frozen ruff check .") == 1
    assert "Run test suite with informational coverage" in python_job
    assert "COVERAGE_FILE: ${{ runner.temp }}/agent-relay.coverage" in python_job
    assert "--cov=agent_relay" in python_job
    assert "--cov-report=term-missing" in python_job
    assert '-m "not integration"' in python_job
    assert "--cov-fail-under" not in python_job


def test_ci_has_one_public_workflow_without_legacy_windows_diagnostics() -> None:
    assert {path.name for path in WORKFLOW_DIR.glob("*.yml")} == {"ci.yml"}


def test_ci_keeps_docker_image_smoke_without_container_ui_e2e() -> None:
    workflow = WORKFLOW.read_text()

    assert "  container:" in workflow
    assert "platform: linux/amd64" in workflow
    assert "platform: linux/arm64" in workflow
    assert "Verify image contract" in workflow
    assert "Smoke-test both roles" in workflow
    assert "PLATFORM: ${{ matrix.platform }}" in workflow
    assert 'docker run --rm --platform="$PLATFORM" "$IMAGE" --help' in workflow
    assert 'docker run --rm --platform="$PLATFORM" "$IMAGE" --version' in workflow

    assert "  container-e2e:" not in workflow
    assert "Dockerfile.e2e-client" not in workflow
    assert "gate4-evidence" not in workflow
    assert "gate4-computer-use-evidence" not in workflow
    assert "Run ten fresh hardened Gate 4 scenarios" not in workflow


def test_ci_smokes_compose_server_with_native_linux_agent_status() -> None:
    workflow = WORKFLOW.read_text()
    job = workflow.split("  relay-compose-link:", 1)[1].split(
        "\n  e2e-linux:", 1
    )[0]

    assert "name: Relay Compose Link - Server / Linux Agent" in job
    assert "runs-on: ubuntu-24.04" in job
    assert 'installer: "true"' in job
    assert "docker compose --env-file \"$env_file\" config --quiet" in job
    assert "docker compose --env-file \"$env_file\" up --build --detach" in job
    assert "scripts/relay_compose_link.py" in job
    assert "relay_device_status" in job
    assert "docker compose --env-file \"$env_file\" down" in job
    assert "relay_system_ping" not in job
    assert "relay_terminal_exec" not in job


def test_ci_labels_native_gates_by_capability() -> None:
    workflow = WORKFLOW.read_text()

    assert "name: Docker image smoke (${{ matrix.platform }})" in workflow
    assert "name: Linux Terminal end-to-end" in workflow
    assert "name: Linux Browser end-to-end" in workflow
    assert "name: Linux CUA end-to-end" in workflow
    assert "name: Windows Terminal end-to-end" in workflow
    assert "name: Windows Browser end-to-end" in workflow
    assert "name: Native Linux" not in workflow
    assert "name: Native Windows" not in workflow

    linux_job = workflow.split("  e2e-linux:", 1)[1].split(
        "\n  e2e-windows-terminal:", 1
    )[0]
    windows_job = workflow.split("  e2e-windows-terminal:", 1)[1].split(
        "\n  e2e-windows-browser:", 1
    )[0]
    browser_job = workflow.split("  e2e-windows-browser:", 1)[1]

    for job in (linux_job, windows_job, browser_job):
        assert "docker run" not in job.lower()
        assert "docker.sock" not in job.lower()
        assert "--privileged" not in job.lower()

    assert "scripts/linux_e2e.py" in linux_job
    assert "scripts/windows_e2e.py" in windows_job
    assert "scripts/windows_browser_e2e.py" in browser_job


def test_terminal_e2e_jobs_use_the_platform_installer_path() -> None:
    workflow = WORKFLOW.read_text()

    linux_job = workflow.split("  e2e-linux:", 1)[1].split(
        "\n  e2e-linux-browser:", 1
    )[0]
    windows_job = workflow.split("  e2e-windows-terminal:", 1)[1].split(
        "\n  e2e-windows-cua:", 1
    )[0]

    assert 'installer: "true"' in linux_job
    assert 'installer: "true"' in windows_job
    assert "installer-linux:" not in workflow
    assert "installer-windows:" not in workflow


def test_docker_matrix_uses_native_runner_for_each_architecture() -> None:
    workflow = WORKFLOW.read_text()
    container_job = workflow.split("  container:", 1)[1].split(
        "\n  e2e-linux:", 1
    )[0]

    assert "runs-on: ${{ matrix.runner }}" in container_job
    assert "needs: python" not in container_job
    assert "runner: ubuntu-24.04" in container_job
    assert "runner: ubuntu-24.04-arm" in container_job
    assert "Install QEMU" not in container_job
    assert "setup-qemu-action" not in container_job


def test_terminal_native_jobs_share_platform_independent_ci_gates() -> None:
    workflow = WORKFLOW.read_text()
    linux_job = workflow.split("  e2e-linux:", 1)[1].split(
        "\n  e2e-linux-browser:", 1
    )[0]
    windows_job = workflow.split("  e2e-windows-terminal:", 1)[1].split(
        "\n  e2e-windows-browser:", 1
    )[0]

    for job in (linux_job, windows_job):
        assert "Verify checked-out commit" in job
        assert "uv run --frozen ruff check ." not in job
        assert "tests/test_runner.py" in job
        assert "uv run --frozen pytest -q -m integration" in job
        assert "if: always()" in job

    assert "EXPECTED_SHA: ${{ github.event.pull_request.head.sha || github.sha }}" in linux_job
    assert "scripts/linux_e2e.py" in linux_job
    assert "tests/test_windows_installer.py" in windows_job
    assert "test_init_agent_from_server_uses_the_effective_custom_token_source" in windows_job
    assert "scripts/windows_e2e.py" in windows_job


def test_ci_externalizes_evidence_validation_and_cua_platform_helpers() -> None:
    workflow = WORKFLOW.read_text()
    expected_profiles = {
        "e2e-linux": "linux-terminal",
        "e2e-linux-browser": "linux-browser",
        "e2e-linux-cua": "linux-cua",
        "e2e-windows-terminal": "windows-terminal",
        "e2e-windows-cua": "windows-cua",
        "e2e-windows-browser": "windows-browser",
    }
    for job_name, profile in expected_profiles.items():
        job = workflow.split(f"  {job_name}:", 1)[1].split("\n  e2e-", 1)[0]
        assert "python scripts/validate_e2e_evidence.py" in job
        assert f"--profile {profile}" in job

    assert "scripts/probe_cua_driver.py --platform linux" in workflow
    assert "scripts/probe_cua_driver.py --platform windows" in workflow
    assert "scripts/windows_install_cua_driver.ps1" in workflow
    assert "python - <<'PY'" not in workflow
    assert "@' | uv run --frozen python -" not in workflow


def test_ci_uses_one_closed_locked_python_setup_action() -> None:
    workflow = WORKFLOW.read_text()
    setup = PYTHON_SETUP.read_text()

    assert workflow.count("uses: ./.github/actions/setup-python") == 8
    assert "astral-sh/setup-uv@" not in workflow
    assert "astral-sh/setup-uv@11f9893b081a58869d3b5fccaea48c9e9e46f990" in setup
    assert 'default: "base"' in setup
    assert '"base")' in setup
    assert '"browser")' in setup
    assert '"computer")' in setup
    assert "uv lock --check" in setup
    assert "uv sync --locked --extra browser --extra computer" in setup
    assert "AGENT_RELAY_ARCHIVE_SOURCE" in setup
    assert "Compress-Archive" in setup
    assert "tar --exclude=.git" in setup
    assert "scripts/install.sh" in setup
    assert "install.ps1" in setup
    assert setup.count("if: inputs.installer != 'true'") == 3
    assert "AGENT_RELAY_SYNC_ROOT" in setup
    assert "AGENT_RELAY_SYNC_PROFILE" in setup
    assert 'printf \'%s\\n\' "$(dirname "$uv_path")" >> "$GITHUB_PATH"' in setup
    assert "RELAY_E2E_AGENT_RELAY_COMMAND" in setup

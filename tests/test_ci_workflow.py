from __future__ import annotations

from pathlib import Path

WORKFLOW_DIR = Path(__file__).parents[1] / ".github/workflows"
WORKFLOW = WORKFLOW_DIR / "ci.yml"


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
    assert 'docker run --rm --platform="$PLATFORM" "$IMAGE" server --help' in workflow
    assert 'docker run --rm --platform="$PLATFORM" "$IMAGE" agent --help' in workflow

    assert "  container-e2e:" not in workflow
    assert "Dockerfile.e2e-client" not in workflow
    assert "gate4-evidence" not in workflow
    assert "gate4-computer-use-evidence" not in workflow
    assert "Run ten fresh hardened Gate 4 scenarios" not in workflow


def test_ci_labels_native_gates_by_capability() -> None:
    workflow = WORKFLOW.read_text()

    assert "name: Docker image smoke (${{ matrix.platform }})" in workflow
    assert "name: Native Linux Terminal end-to-end" in workflow
    assert "name: Native Linux Browser end-to-end" in workflow
    assert "name: Native Linux CUA end-to-end" in workflow
    assert "name: Native Windows Terminal end-to-end" in workflow
    assert "name: Native Windows Browser end-to-end" in workflow

    linux_job = workflow.split("  e2e-linux-native:", 1)[1].split(
        "\n  e2e-windows-native:", 1
    )[0]
    windows_job = workflow.split("  e2e-windows-native:", 1)[1].split(
        "\n  e2e-windows-browser:", 1
    )[0]
    browser_job = workflow.split("  e2e-windows-browser:", 1)[1]

    for job in (linux_job, windows_job, browser_job):
        assert "docker run" not in job.lower()
        assert "docker.sock" not in job.lower()
        assert "--privileged" not in job.lower()

    assert "scripts/native_e2e.py" in linux_job
    assert "scripts/windows_e2e.py" in windows_job
    assert "scripts/windows_browser_e2e.py" in browser_job


def test_docker_matrix_uses_native_runner_for_each_architecture() -> None:
    workflow = WORKFLOW.read_text()
    container_job = workflow.split("  container:", 1)[1].split(
        "\n  e2e-linux-native:", 1
    )[0]

    assert "runs-on: ${{ matrix.runner }}" in container_job
    assert "runner: ubuntu-24.04" in container_job
    assert "runner: ubuntu-24.04-arm" in container_job
    assert "Install QEMU" not in container_job
    assert "setup-qemu-action" not in container_job


def test_terminal_native_jobs_share_platform_independent_ci_gates() -> None:
    workflow = WORKFLOW.read_text()
    linux_job = workflow.split("  e2e-linux-native:", 1)[1].split(
        "\n  e2e-linux-browser:", 1
    )[0]
    windows_job = workflow.split("  e2e-windows-native:", 1)[1].split(
        "\n  e2e-windows-browser:", 1
    )[0]

    for job in (linux_job, windows_job):
        assert "Verify checked-out commit" in job
        assert "uv run --frozen ruff check ." in job
        assert "tests/test_runner.py" in job
        assert "uv run --frozen pytest -q -m integration" in job
        assert "if: always()" in job

    assert "EXPECTED_SHA: ${{ github.event.pull_request.head.sha || github.sha }}" in linux_job
    assert "scripts/native_e2e.py" in linux_job
    assert "scripts/windows_e2e.py" in windows_job

from __future__ import annotations

import importlib.util
import os
import sys
from io import BytesIO
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "windows_e2e.py"
WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"


def _load_harness():
    spec = importlib.util.spec_from_file_location("windows_e2e", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generate_credentials_are_distinct_and_bounded() -> None:
    harness = _load_harness()

    agent_token, control_token = harness.generate_credentials()

    assert agent_token and control_token
    assert agent_token != control_token
    assert len(agent_token) <= harness.MAX_TOKEN_LENGTH
    assert len(control_token) <= harness.MAX_TOKEN_LENGTH


def test_windows_commands_are_fixed_loopback_module_entrypoints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = _load_harness()
    monkeypatch.delenv("RELAY_E2E_AGENT_RELAY_COMMAND", raising=False)

    server = harness.server_command(23456)
    agent = harness.agent_command(23456, tmp_path)

    assert server == [
        sys.executable,
        "-m",
        "agent_relay.server",
        "--host",
        "127.0.0.1",
        "--port",
        "23456",
    ]
    assert agent == [sys.executable, "-m", "agent_relay.agent"]
    assert all(isinstance(item, str) for item in server + agent)
    assert "--host=0.0.0.0" not in server
    assert "--shell" not in server + agent


def test_windows_commands_can_use_the_installed_relay_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = _load_harness()
    installed = str(tmp_path / "agent-relay.exe")
    monkeypatch.setenv("RELAY_E2E_AGENT_RELAY_COMMAND", installed)

    assert harness.server_command(23456) == [installed, "server"]
    assert harness.agent_command(23456, tmp_path) == [installed, "agent"]

    environment = harness.minimal_environment(tmp_path / "home", {})
    assert "PYTHONPATH" not in environment


def test_minimal_environment_does_not_inherit_unrelated_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    harness = _load_harness()
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross-boundary")
    monkeypatch.setenv("HOME", "untrusted-home")

    environment = harness.minimal_environment(
        tmp_path,
        {"RELAY_AGENT_ID": "windows-e2e"},
    )

    assert environment["RELAY_AGENT_ID"] == "windows-e2e"
    assert environment["USERPROFILE"] == str(tmp_path)
    assert environment["TEMP"] == str(tmp_path)
    assert environment["TMP"] == str(tmp_path)
    assert "OPENAI_API_KEY" not in environment
    assert environment["HOME"] == str(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="tests the non-Windows guard")
def test_windows_job_requires_windows() -> None:
    harness = _load_harness()

    with pytest.raises(harness.WindowsE2EError, match="requires Windows"):
        harness.WindowsJob()


def test_windows_job_accounting_structure_includes_all_kernel_fields() -> None:
    harness = _load_harness()

    fields = dict(harness._BasicAccountingInformation._fields_)

    assert fields["ThisPeriodTotalKernelTime"] is harness._LargeInteger


def test_job_termination_closes_handle_when_accounting_fails() -> None:
    harness = _load_harness()
    job = object.__new__(harness.WindowsJob)
    job._handle = object()

    class Kernel:
        def __init__(self) -> None:
            self.closed = False

        def TerminateJobObject(self, _handle, _exit_code) -> bool:
            return True

        def CloseHandle(self, _handle) -> bool:
            self.closed = True
            return True

    kernel = Kernel()
    job._kernel32 = kernel

    def fail_accounting() -> int:
        raise harness.WindowsE2EError("accounting unavailable")

    job.active_processes = fail_accounting
    with pytest.raises(harness.WindowsE2EError, match="accounting unavailable"):
        job.terminate(timeout=0.1, processes=[])

    assert kernel.closed is True
    assert job._handle is None


@pytest.mark.skipif(os.name == "nt", reason="tests the non-Windows guard")
def test_run_scenario_requires_windows() -> None:
    harness = _load_harness()

    with pytest.raises(harness.WindowsE2EError, match="requires Windows"):
        harness.run_scenario()


def test_lifecycle_preserves_primary_failure_when_cleanup_fails() -> None:
    harness = _load_harness()
    lifecycle = harness.WindowsLifecycle()

    def fail_cleanup() -> None:
        raise RuntimeError("cleanup failure")

    lifecycle.add_cleanup(fail_cleanup)
    with pytest.raises(ValueError, match="primary failure"):
        with lifecycle:
            raise ValueError("primary failure")

    assert lifecycle.cleanup_error is not None
    assert str(lifecycle.cleanup_error) == "Windows E2E cleanup failed"
    assert lifecycle.cleanup_error.__cause__ is not None
    assert str(lifecycle.cleanup_error.__cause__) == "cleanup failure"


def test_cleanup_closes_parent_diagnostic_streams() -> None:
    harness = _load_harness()
    lifecycle = harness.WindowsLifecycle()

    class Stream:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class Process:
        def __init__(self) -> None:
            self.stderr = Stream()

    process = Process()
    lifecycle.processes.append(process)

    lifecycle.close_diagnostic_streams()

    assert process.stderr.closed is True


def test_cleanup_failure_records_bounded_resource_label() -> None:
    harness = _load_harness()
    lifecycle = harness.WindowsLifecycle()

    lifecycle.add_cleanup(
        lambda: (_ for _ in ()).throw(RuntimeError("secret cleanup detail")),
        label="temporary-directory",
    )

    with pytest.raises(harness.WindowsE2EError, match="cleanup failed"):
        lifecycle.cleanup()

    assert lifecycle.cleanup_failures == ["temporary-directory"]


def test_child_diagnostics_use_closed_categories_without_echoing_exception_names(
    tmp_path: Path,
) -> None:
    harness = _load_harness()
    diagnostic = tmp_path / "child.stderr.log"
    diagnostic.write_text(
        "Traceback (most recent call last):\n"
        "  File 'secret-path', line 1\n"
        "SecretCredentialError: token-value\n",
        encoding="utf-8",
    )

    category = harness._diagnostic_category(diagnostic)

    assert category == "child traceback"
    assert "SecretCredentialError" not in category
    assert "token-value" not in category


def test_startup_diagnostics_preserve_only_bounded_categories(tmp_path: Path) -> None:
    harness = _load_harness()
    diagnostic = tmp_path / "child.stderr.log"
    diagnostic.write_text(
        "computer startup failed: phase=process-spawn category=os-error\n"
        "cua provider discovery failed: category=invalid-inventory\n"
        "secret-path token-value\n",
        encoding="utf-8",
    )

    category = harness._diagnostic_category(diagnostic)

    assert category == "computer startup failure: phase=process-spawn category=os-error"
    assert "secret-path" not in category
    assert "token-value" not in category


def test_startup_phase_diagnostic_uses_latest_bounded_phase(tmp_path: Path) -> None:
    harness = _load_harness()
    diagnostic = tmp_path / "child.stderr.log"
    diagnostic.write_text(
        "computer startup phase: windows-privacy-skip\n"
        "computer startup phase: windows-daemon-spawn\n"
        "computer startup phase: windows-daemon-probe\n",
        encoding="utf-8",
    )

    category = harness._diagnostic_category(diagnostic)

    assert category == "computer startup windows daemon probe"
    assert "windows-privacy-skip" not in category


def test_diagnostic_drain_is_bounded(tmp_path: Path) -> None:
    harness = _load_harness()
    diagnostic = tmp_path / "child.stderr.log"

    harness._drain_diagnostic(
        BytesIO(b"prefix\n" + b"x" * (harness.MAX_DIAGNOSTIC_BYTES * 2)),
        diagnostic,
    )

    assert diagnostic.stat().st_size == harness.MAX_DIAGNOSTIC_BYTES
    assert diagnostic.read_bytes().startswith(b"prefix\n")


def test_cleanup_diagnostics_use_a_closed_category() -> None:
    harness = _load_harness()

    category = harness._cleanup_category(RuntimeError("secret cleanup path"))

    assert category == "cleanup failed"
    assert "secret" not in category


def test_native_lifecycle_scenario_contains_server_restart_gate() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    for phase in (
        '"server-stop"',
        '"server-unavailable"',
        '"server-restart"',
        '"post-restart-core-scenario"',
    ):
        assert phase in source
    assert "if server.poll() is None:" in source


def test_write_artifact_rejects_oversized_payload(tmp_path: Path) -> None:
    harness = _load_harness()

    with pytest.raises(harness.WindowsE2EError, match="oversized"):
        harness.write_artifact(
            tmp_path,
            "output.log",
            b"x" * (harness.MAX_ARTIFACT_BYTES + 1),
        )


def test_write_artifact_rejects_preexisting_symlink(tmp_path: Path) -> None:
    harness = _load_harness()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    target = tmp_path / "outside.log"
    target.write_text("unchanged", encoding="utf-8")
    try:
        (evidence / "output.log").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this host")

    with pytest.raises((harness.WindowsE2EError, FileExistsError, OSError, ValueError)):
        harness.write_artifact(evidence, "output.log", b"bounded\n")

    assert target.read_text(encoding="utf-8") == "unchanged"


def test_ci_defines_bounded_native_windows_gate_without_docker_or_ui() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "  e2e-windows-terminal:" in workflow
    job = workflow.split("  e2e-windows-terminal:", 1)[1].split("\n  e2e-", 1)[0]

    assert "name: Windows Terminal end-to-end" in job
    assert "needs: python" in job
    assert "runs-on: windows-2025" in job
    assert "runs-on: windows-2022" not in job
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in job
    assert "Verify checked-out commit" in job
    assert "uses: ./.github/actions/setup-python" in job
    assert "uv run --frozen ruff check ." not in job
    assert "uv run --frozen python -m pytest -q" in job
    for test_path in (
        "tests/test_windows_e2e.py",
        "tests/test_installers.py::test_windows_path_refresh_preserves_session_entries",
        "tests/test_installers.py::test_windows_agent_configuration_probe_fails_closed",
        "tests/test_cli_uninstall.py::test_windows_uninstall_retries_real_locked_executable",
        "tests/test_cli_uninstall.py::test_windows_uninstall_real_process_lock_boundary",
    ):
        assert test_path in job
    assert "tests/test_linux_e2e.py" not in job
    assert "tests/test_runner.py" not in job
    assert "test_init_agent_from_server_uses_the_effective_dotenv_token" not in job
    assert "git_search_skips_relative_default_path_entries" not in job
    assert "uv run --frozen python -m pytest -q -m integration" not in job
    assert "uv run --frozen python scripts/windows_e2e.py" in job
    assert "python scripts/validate_e2e_evidence.py" in job
    assert "--profile windows-terminal" in job
    assert "windows-evidence" in job
    assert "docker" not in job.lower()
    assert "browser" not in job.lower()
    assert "computer" not in job.lower()
    assert "OPENAI_API_KEY" not in job
    assert "TOKEN" not in job
    assert "id: validate-windows-evidence" in job
    assert "if: always()" in job

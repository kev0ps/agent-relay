from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "linux_e2e.py"
WINDOWS_SCRIPT = Path(__file__).parents[1] / "scripts" / "windows_e2e.py"
WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"


def _load_harness():
    spec = importlib.util.spec_from_file_location("linux_e2e", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generate_credentials_are_distinct_and_bounded() -> None:
    harness = _load_harness()

    agent_token, control_token = harness.generate_credentials()

    assert isinstance(agent_token, str)
    assert isinstance(control_token, str)
    assert agent_token
    assert control_token
    assert agent_token != control_token
    assert len(agent_token) <= 128
    assert len(control_token) <= 128


def test_native_commands_use_fixed_module_entrypoints(
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


def test_native_commands_can_use_the_installed_relay_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = _load_harness()
    installed = str(tmp_path / "agent-relay")
    monkeypatch.setenv("RELAY_E2E_AGENT_RELAY_COMMAND", installed)

    assert harness.server_command(23456) == [installed, "server"]
    assert harness.agent_command(23456, tmp_path) == [installed, "agent"]

    environment = harness._minimal_environment(tmp_path / "home", {})
    assert "PYTHONPATH" not in environment


def test_minimal_environment_preserves_only_cua_driver_runtime_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = _load_harness()
    monkeypatch.setenv("CUA_DRIVER_RS_HOME", str(tmp_path / "driver-home"))
    monkeypatch.setenv("CUA_DRIVER_RS_INSTALL_DIR", str(tmp_path / "driver-bin"))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross-boundary")

    environment = harness._minimal_environment(tmp_path / "child-home", {})

    assert environment["CUA_DRIVER_RS_HOME"] == str(tmp_path / "driver-home")
    assert environment["CUA_DRIVER_RS_INSTALL_DIR"] == str(tmp_path / "driver-bin")
    assert "OPENAI_API_KEY" not in environment


def test_lifecycle_preserves_primary_failure_when_cleanup_fails() -> None:
    harness = _load_harness()
    lifecycle = harness.NativeLifecycle()

    def fail_cleanup() -> None:
        raise RuntimeError("cleanup failure")

    lifecycle.add_cleanup(fail_cleanup)
    with pytest.raises(ValueError, match="primary failure"):
        with lifecycle:
            raise ValueError("primary failure")

    assert lifecycle.cleanup_error is not None
    assert str(lifecycle.cleanup_error) == "native E2E cleanup failed"
    assert lifecycle.cleanup_error.__cause__ is not None
    assert str(lifecycle.cleanup_error.__cause__) == "cleanup failure"


def test_lifecycle_reports_cleanup_failure_without_primary_failure() -> None:
    harness = _load_harness()
    lifecycle = harness.NativeLifecycle()

    lifecycle.add_cleanup(lambda: (_ for _ in ()).throw(RuntimeError("cleanup failure")))

    with pytest.raises(RuntimeError, match="native E2E cleanup failed"):
        with lifecycle:
            pass


def test_wait_for_process_exit_rejects_unbounded_timeout() -> None:
    harness = _load_harness()

    with pytest.raises(ValueError, match="timeout"):
        harness.wait_for_process_exit(None, timeout=0)


def test_lifecycle_installs_and_restores_termination_handlers() -> None:
    """Owned children must be cleaned when the harness receives termination."""
    harness = _load_harness()
    previous = {
        signal.SIGINT: signal.getsignal(signal.SIGINT),
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
    }
    lifecycle = harness.NativeLifecycle()

    try:
        lifecycle.install_signal_handlers()
        assert signal.getsignal(signal.SIGINT) != previous[signal.SIGINT]
        assert signal.getsignal(signal.SIGTERM) != previous[signal.SIGTERM]
    finally:
        lifecycle.cleanup()

    assert signal.getsignal(signal.SIGINT) == previous[signal.SIGINT]
    assert signal.getsignal(signal.SIGTERM) == previous[signal.SIGTERM]


def test_sigterm_inside_lifecycle_runs_cleanup_and_restores_handlers() -> None:
    """A termination signal must become a controlled, cleaned interruption."""
    harness = _load_harness()
    previous = signal.getsignal(signal.SIGTERM)
    cleaned: list[bool] = []
    lifecycle = harness.NativeLifecycle()
    lifecycle.install_signal_handlers()
    lifecycle.add_cleanup(lambda: cleaned.append(True))

    with pytest.raises(KeyboardInterrupt, match="received signal"):
        with lifecycle:
            signal.raise_signal(signal.SIGTERM)

    assert cleaned == [True]
    assert signal.getsignal(signal.SIGTERM) == previous


def test_partial_signal_handler_installation_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed second registration must restore the first handler."""
    harness = _load_harness()
    previous = {
        signal.SIGINT: signal.getsignal(signal.SIGINT),
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
    }
    original_signal = signal.signal

    def flaky_signal(signum: signal.Signals, handler: object) -> object:
        if signum == signal.SIGTERM:
            raise RuntimeError("injected handler failure")
        return original_signal(signum, handler)  # type: ignore[arg-type]

    monkeypatch.setattr(harness.signal, "signal", flaky_signal)
    lifecycle = harness.NativeLifecycle()
    with pytest.raises(RuntimeError, match="injected handler failure"):
        lifecycle.install_signal_handlers()

    assert signal.getsignal(signal.SIGINT) == previous[signal.SIGINT]
    assert signal.getsignal(signal.SIGTERM) == previous[signal.SIGTERM]


def test_failed_lifecycle_enter_restores_signal_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback cleanup covers a failure before context-manager entry."""
    harness = _load_harness()
    previous = {
        signal.SIGINT: signal.getsignal(signal.SIGINT),
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
    }

    def fail_enter(_self: object) -> object:
        raise KeyboardInterrupt("injected enter failure")

    monkeypatch.setattr(harness.NativeLifecycle, "__enter__", fail_enter)
    with pytest.raises(KeyboardInterrupt, match="injected enter failure"):
        harness.run_scenario()

    assert signal.getsignal(signal.SIGINT) == previous[signal.SIGINT]
    assert signal.getsignal(signal.SIGTERM) == previous[signal.SIGTERM]


def test_terminate_process_group_kills_descendant_after_leader_exit() -> None:
    """Group cleanup must not rely on the leader remaining alive."""
    harness = _load_harness()
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess,sys; child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); print(child.pid, flush=True)",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert process.stdout is not None
    child_pid = int(process.stdout.readline().strip())
    process.wait(timeout=5)

    try:
        assert process.poll() is not None
        harness.terminate_process_group(process, timeout=1, process_group_id=process.pid)
        assert not harness._process_group_has_live_members(process.pid)
    finally:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_write_success_rejects_preexisting_symlink(tmp_path: Path) -> None:
    """Evidence writers must fail closed instead of following symlinks."""
    harness = _load_harness()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("unchanged", encoding="utf-8")
    (evidence / "success.json").symlink_to(target)

    with pytest.raises((FileExistsError, OSError, ValueError)):
        harness._write_success(evidence)

    assert target.read_text(encoding="utf-8") == "unchanged"


def test_write_output_rejects_preexisting_symlink(tmp_path: Path) -> None:
    """Failure diagnostics must use the same no-follow artifact policy."""
    harness = _load_harness()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    target = tmp_path / "outside.log"
    target.write_text("unchanged", encoding="utf-8")
    (evidence / "output.log").symlink_to(target)

    with pytest.raises((FileExistsError, OSError, ValueError)):
        harness._write_artifact(evidence, "output.log", b"bounded\n")

    assert target.read_text(encoding="utf-8") == "unchanged"


def test_failed_cleanup_does_not_leave_success_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed lifecycle must never publish a passing evidence marker."""
    harness = _load_harness()

    class FakeProcess:
        pid = 12345

        def __init__(self) -> None:
            self.stopped = False

        def poll(self) -> int | None:
            return 0 if self.stopped else None

    monkeypatch.setattr(harness, "_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(harness, "_spawn", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(
        harness.portable_scenarios,
        "run_core_scenario",
        lambda *_args, **_kwargs: None,
    )

    def fake_terminate(process: FakeProcess, **_kwargs: object) -> None:
        process.stopped = True

    endpoint_probes = 0

    def fake_endpoint_available(*_args: object, **_kwargs: object) -> bool:
        nonlocal endpoint_probes
        endpoint_probes += 1
        return endpoint_probes >= 2

    monkeypatch.setattr(harness, "_server_endpoint_available", fake_endpoint_available)
    monkeypatch.setattr(harness, "terminate_process_group", fake_terminate)
    monkeypatch.setattr(
        harness.NativeLifecycle,
        "cleanup",
        lambda _self: (_ for _ in ()).throw(
            harness.NativeE2EError("native E2E cleanup failed")
        ),
    )

    with pytest.raises(harness.NativeE2EError, match="cleanup failed"):
        harness.run_scenario(tmp_path / "evidence")

    assert not (tmp_path / "evidence" / "success.json").exists()


def test_primary_failure_reports_secondary_cleanup_without_raw_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cleanup diagnostics stay fixed and do not replace the primary failure."""
    harness = _load_harness()

    class FakeProcess:
        pid = 12345

        def poll(self) -> int | None:
            return None

    monkeypatch.setattr(harness, "_status", lambda *_args, **_kwargs: None)

    def fake_spawn(_argv: object, *, lifecycle: Any, **_kwargs: object) -> FakeProcess:
        process = FakeProcess()
        lifecycle.own_process(process)
        return process

    monkeypatch.setattr(harness, "_spawn", fake_spawn)
    monkeypatch.setattr(
        harness.portable_scenarios,
        "run_core_scenario",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("primary failure")),
    )
    monkeypatch.setattr(
        harness,
        "terminate_process_group",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("secret cleanup detail")
        ),
    )

    evidence = tmp_path / "evidence"
    with pytest.raises(ValueError, match="primary failure"):
        harness.run_scenario(evidence, output_file=evidence / "output.log")

    stderr = capsys.readouterr().err
    assert "Native Linux E2E cleanup failed." in stderr
    assert "secret cleanup detail" not in stderr
    assert "Native Linux E2E cleanup failed." in (evidence / "output.log").read_text(
        encoding="ascii"
    )


def test_terminal_harnesses_share_server_restart_lifecycle() -> None:
    sources = (
        SCRIPT.read_text(encoding="utf-8"),
        WINDOWS_SCRIPT.read_text(encoding="utf-8"),
    )

    phases = (
        '"server-start"',
        '"agent-start"',
        '"core-scenario"',
        '"agent-stop"',
        '"offline-detection"',
        '"agent-reconnect"',
        '"reconnected-core-scenario"',
        '"server-stop"',
        '"server-unavailable"',
        '"server-restart"',
        '"post-restart-core-scenario"',
    )
    for source in sources:
        for phase in phases:
            assert phase in source
        assert source.count("portable_scenarios.run_core_scenario(") == 3



def test_ci_defines_bounded_native_linux_gate_without_container_privileges() -> None:
    """The native gate must prove the real path without Docker or credentials."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "  e2e-linux:" in workflow
    job = workflow.split("  e2e-linux:", 1)[1]

    assert "name: Linux Terminal end-to-end" in job
    assert "needs: python" in job
    assert "runs-on: ubuntu-24.04" in job
    assert "uses: ./.github/actions/setup-python" in job
    assert "uv run --frozen python scripts/linux_e2e.py" in job
    assert "python scripts/validate_e2e_evidence.py" in job
    assert "--profile linux-terminal" in job
    assert "docker" not in job.lower()
    assert "privileged" not in job.lower()
    assert "docker.sock" not in job.lower()
    assert "OPENAI_API_KEY" not in job
    assert "TOKEN" not in job
    assert "id: validate-native-evidence" in job
    assert "if: always()" in job
    assert "steps.validate-native-evidence.outcome == 'success'" in job

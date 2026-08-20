from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.e2e.platform.posix import (
    PosixE2EError,
    PosixProcessManager,
    _process_group_has_live_members,
    terminate_process_group,
)

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="requires POSIX")


def test_minimal_environment_preserves_only_driver_runtime_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CUA_DRIVER_RS_HOME", str(tmp_path / "driver-home"))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross-boundary")

    environment = PosixProcessManager().minimal_environment(tmp_path / "home", {})

    assert environment["CUA_DRIVER_RS_HOME"] == str(tmp_path / "driver-home")
    assert "OPENAI_API_KEY" not in environment


@POSIX_ONLY
def test_manager_installs_and_restores_termination_handlers() -> None:
    previous = {
        signal.SIGINT: signal.getsignal(signal.SIGINT),
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
    }
    manager = PosixProcessManager()

    manager.prepare()
    assert signal.getsignal(signal.SIGINT) != previous[signal.SIGINT]
    assert signal.getsignal(signal.SIGTERM) != previous[signal.SIGTERM]
    manager.cleanup()

    assert signal.getsignal(signal.SIGINT) == previous[signal.SIGINT]
    assert signal.getsignal(signal.SIGTERM) == previous[signal.SIGTERM]


@POSIX_ONLY
def test_cleanup_runs_all_owned_cleanups_and_reports_a_bounded_error() -> None:
    manager = PosixProcessManager()
    events: list[str] = []
    manager.add_cleanup(lambda: events.append("last"))
    manager.add_cleanup(
        lambda: (_ for _ in ()).throw(RuntimeError("secret cleanup detail"))
    )
    manager.add_cleanup(lambda: events.append("first"))

    with pytest.raises(PosixE2EError, match="cleanup failed") as raised:
        manager.cleanup()

    assert events == ["first", "last"]
    assert "secret cleanup detail" not in str(raised.value)


@POSIX_ONLY
def test_terminate_process_group_kills_descendant_after_leader_exit() -> None:
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
        terminate_process_group(process, timeout=1, process_group_id=process.pid)
        assert not _process_group_has_live_members(process.pid)
    finally:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

"""POSIX process groups and environment primitives for native E2E runs."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ..common import E2EError

POLL_INTERVAL_SECONDS = 0.1
PROCESS_STOP_TIMEOUT_SECONDS = 5.0


class PosixE2EError(E2EError):
    """A bounded POSIX adapter failure."""


def _process_group_has_live_members(group_id: int) -> bool:
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        try:
            os.killpg(group_id, 0)
        except ProcessLookupError:
            return False
        return True
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (
                (entry / "stat")
                .read_text(encoding="ascii")
                .rsplit(") ", 1)[1]
                .split()
            )
            state = fields[0]
            member_group_id = int(fields[2])
        except (OSError, IndexError, ValueError):
            continue
        if member_group_id == group_id and state != "Z":
            return True
    return False


def terminate_process_group(
    process: subprocess.Popen[Any],
    *,
    timeout: float = PROCESS_STOP_TIMEOUT_SECONDS,
    process_group_id: int | None = None,
) -> None:
    """Terminate one owned process group with bounded TERM then KILL."""
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("timeout must be positive")
    group_id = process.pid if process_group_id is None else process_group_id

    def wait_for_group_exit() -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None and not _process_group_has_live_members(group_id):
                return True
            time.sleep(POLL_INTERVAL_SECONDS)
        return process.poll() is not None and not _process_group_has_live_members(group_id)

    try:
        os.killpg(group_id, signal.SIGTERM)
    except ProcessLookupError:
        if process.poll() is not None:
            return
        raise PosixE2EError("process group disappeared during cleanup") from None
    if wait_for_group_exit():
        return
    try:
        os.killpg(group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if not wait_for_group_exit():
        raise PosixE2EError("process group cleanup timed out")


class PosixProcessManager:
    """Own POSIX children while exposing only the shared process contract."""

    name = "Linux"
    device_id = "native-e2e-agent"
    run_prefix = "native"
    cua_run_prefix = "linux-cua"

    def __init__(self) -> None:
        self._cleanups: list[Callable[[], None]] = []
        self._previous_handlers: dict[signal.Signals, Any] = {}
        self._cleaned = False

    def prepare(self) -> None:
        if os.name != "posix":
            raise PosixE2EError("Linux harness requires POSIX")
        if self._previous_handlers:
            return

        def interrupted(signum: int, _frame: Any) -> None:
            raise KeyboardInterrupt(f"received signal {signum}")

        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                self._previous_handlers[signum] = signal.signal(signum, interrupted)
        except BaseException:
            for signum, handler in self._previous_handlers.items():
                try:
                    signal.signal(signum, handler)
                except BaseException:
                    pass
            self._previous_handlers.clear()
            raise

    def add_cleanup(self, cleanup: Callable[[], None]) -> None:
        self._cleanups.append(cleanup)

    def minimal_environment(
        self,
        home: Path,
        values: dict[str, str],
    ) -> dict[str, str]:
        environment = {
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "PATH": os.environ.get("PATH", ""),
            "PYTHONUNBUFFERED": "1",
        }
        environment["PYTHONPATH"] = str(Path(__file__).parents[3] / "src")
        if value := os.environ.get("CUA_DRIVER_RS_HOME"):
            environment["CUA_DRIVER_RS_HOME"] = value
        environment.update(values)
        return environment

    def spawn(
        self,
        argv: Sequence[str],
        *,
        environment: dict[str, str],
        cwd: Path,
        label: str,
    ) -> subprocess.Popen[Any]:
        del label
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            shell=False,
        )
        try:
            process_group_id = os.getpgid(process.pid)
        except ProcessLookupError:
            process_group_id = process.pid
        self.add_cleanup(
            lambda process=process, process_group_id=process_group_id: terminate_process_group(
                process,
                process_group_id=process_group_id,
            )
        )
        return process

    def spawn_with_diagnostics(
        self,
        argv: Sequence[str],
        *,
        environment: dict[str, str],
        cwd: Path,
        diagnostic_file: Path,
    ) -> subprocess.Popen[Any]:
        diagnostic_file.parent.mkdir(parents=True, exist_ok=True)
        stream = diagnostic_file.open("xb")
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=stream,
                start_new_session=True,
                shell=False,
            )
        finally:
            stream.close()
        try:
            process_group_id = os.getpgid(process.pid)
        except ProcessLookupError:
            process_group_id = process.pid
        self.add_cleanup(
            lambda process=process, process_group_id=process_group_id: terminate_process_group(
                process,
                process_group_id=process_group_id,
            )
        )
        return process

    def stop(self, process: Any) -> None:
        terminate_process_group(process)

    def expected_pwd(self, workspace: Path) -> str:
        return str(workspace)

    def cleanup(self) -> None:
        if self._cleaned:
            return
        failures: list[BaseException] = []
        for signum in self._previous_handlers:
            try:
                signal.signal(signum, signal.SIG_IGN)
            except BaseException as error:
                failures.append(error)
        for cleanup in reversed(self._cleanups):
            try:
                cleanup()
            except BaseException as error:
                failures.append(error)
        for signum, handler in self._previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except BaseException as error:
                failures.append(error)
        self._cleaned = True
        if failures:
            raise PosixE2EError("Linux E2E cleanup failed") from failures[0]

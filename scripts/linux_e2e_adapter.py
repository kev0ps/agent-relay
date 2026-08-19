"""POSIX process adapter for the shared native E2E harness."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def _load_shared() -> Any:
    name = "_agent_relay_shared_e2e_harness"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    path = Path(__file__).with_name("e2e_harness.py")
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load shared E2E harness")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


harness = _load_shared()
portable_mcp = harness.portable_mcp
portable_oracles = harness.portable_oracles
portable_scenarios = harness.portable_scenarios

DEVICE_ID = "native-e2e-agent"
CORE_CAPABILITIES = harness.CORE_CAPABILITIES
POLL_INTERVAL_SECONDS = harness.POLL_INTERVAL_SECONDS
PROCESS_STOP_TIMEOUT_SECONDS = harness.PROCESS_STOP_TIMEOUT_SECONDS
MAX_TOKEN_LENGTH = harness.MAX_TOKEN_LENGTH


class NativeE2EError(harness.E2EHarnessError):
    """A bounded, non-sensitive Linux harness failure."""


def generate_credentials() -> tuple[str, str]:
    return harness.generate_credentials(NativeE2EError)


def choose_loopback_port() -> int:
    return harness.choose_loopback_port()


def server_command(port: int) -> list[str]:
    return harness.server_command(port)


def agent_command(port: int, workspace: Path) -> list[str]:
    return harness.agent_command(port, workspace)


def wait_for_process_exit(
    process: subprocess.Popen[Any] | None, *, timeout: float
) -> bool:
    return harness.wait_for_process_exit(process, timeout=timeout)


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
    """Terminate one process group with bounded TERM then KILL cleanup."""
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("timeout must be positive")
    group_id = process.pid if process_group_id is None else process_group_id

    def group_exited() -> bool:
        return not _process_group_has_live_members(group_id)

    def wait_for_group_exit() -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None and group_exited():
                return True
            time.sleep(POLL_INTERVAL_SECONDS)
        return process.poll() is not None and group_exited()

    try:
        os.killpg(group_id, signal.SIGTERM)
    except ProcessLookupError:
        if process.poll() is not None:
            return
        raise NativeE2EError("process group disappeared during cleanup") from None
    if wait_for_group_exit():
        return
    try:
        os.killpg(group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if not wait_for_group_exit():
        raise NativeE2EError("process group cleanup timed out")


class NativeLifecycle(harness.Lifecycle):
    """Add POSIX process-group ownership to the shared lifecycle."""

    def __init__(self) -> None:
        super().__init__(NativeE2EError, "Linux E2E cleanup failed")

    def own_process(self, process: subprocess.Popen[Any]) -> None:
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


def _minimal_environment(home: Path, values: dict[str, str]) -> dict[str, str]:
    """Build a small child environment without inheriting unrelated secrets."""
    environment = {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "PATH": os.environ.get("PATH", ""),
        "PYTHONUNBUFFERED": "1",
    }
    if not os.environ.get("RELAY_E2E_AGENT_RELAY_COMMAND"):
        environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    for name in ("CUA_DRIVER_RS_HOME",):
        if value := os.environ.get(name):
            environment[name] = value
    environment.update(values)
    return environment


def _spawn(
    argv: Sequence[str],
    *,
    environment: dict[str, str],
    cwd: Path,
    lifecycle: NativeLifecycle,
    stderr_path: Path | None = None,
) -> subprocess.Popen[Any]:
    """Start one fixed native child in an owned process group."""
    if stderr_path is None:
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
    else:
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        with stderr_path.open("wb") as stream:
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
    lifecycle.own_process(process)
    return process


def _status(
    mcp_url: str,
    control_token: str,
    *,
    connected: bool,
    expected_capabilities: tuple[str, ...] | None = None,
    allow_unenrolled: bool = False,
) -> None:
    harness.status(
        mcp_url,
        control_token,
        device_id=DEVICE_ID,
        connected=connected,
        expected_capabilities=expected_capabilities,
        allow_unenrolled=allow_unenrolled,
    )


def _server_endpoint_available(mcp_url: str, control_token: str) -> bool:
    return harness.server_endpoint_available(mcp_url, control_token)


def _runtime(
    *, mcp_url: str, control_token: str, run_id: str, fixtures_root: Path
) -> Any:
    return harness.runtime_config(
        mcp_url=mcp_url,
        control_token=control_token,
        device_id=DEVICE_ID,
        run_id=run_id,
        fixtures_root=fixtures_root,
    )


def _create_workspace(path: Path) -> None:
    harness.create_workspace(path, readonly_marker=True)


def _write_artifact(evidence_dir: Path, name: str, payload: bytes) -> None:
    if name not in {"output.log", "success.json"}:
        raise NativeE2EError("unsupported evidence file")
    if evidence_dir.is_symlink():
        raise NativeE2EError("unsafe evidence directory")
    evidence_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    if not all(hasattr(os, name) for name in required_flags):
        raise NativeE2EError("safe evidence writing is unavailable")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory_fd = os.open(evidence_dir, directory_flags)
    try:
        directory_metadata = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_metadata.st_mode):
            raise NativeE2EError("unsafe evidence directory")
        file_fd = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | os.O_CLOEXEC,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise NativeE2EError("unsafe evidence file")
            written = 0
            while written < len(payload):
                written += os.write(file_fd, payload[written:])
            os.fchown(file_fd, directory_metadata.st_uid, directory_metadata.st_gid)
            os.fsync(file_fd)
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)


def _write_success(evidence_dir: Path) -> None:
    _write_artifact(evidence_dir, "success.json", b'{"status":"passed"}\n')

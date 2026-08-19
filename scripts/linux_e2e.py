#!/usr/bin/env python3
"""Run the minimal Linux Agent Relay MCP scenario."""

from __future__ import annotations

import argparse
import os
import secrets
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

try:
    from tests.e2e import mcp_client as portable_mcp
    from tests.e2e import oracles as portable_oracles
    from tests.e2e import scenarios as portable_scenarios
except ModuleNotFoundError as error:
    if error.name not in {"tests", "tests.e2e"}:
        raise

    import importlib.util

    def _load_portable(name: str) -> Any:
        dotted = f"_agent_relay_linux_e2e_{name}"
        cached = sys.modules.get(dotted)
        if cached is not None:
            return cached
        target = Path(__file__).parents[1] / "tests" / "e2e" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(dotted, target)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load portable kernel module {name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[dotted] = module
        spec.loader.exec_module(module)
        return module

    portable_mcp = _load_portable("mcp_client")
    portable_oracles = _load_portable("oracles")
    portable_scenarios = _load_portable("scenarios")


DEVICE_ID = "native-e2e-agent"
CORE_CAPABILITIES = ("system.ping", "terminal.exec")
FIXTURE_URL = "http://127.0.0.1:8899/"
POLL_INTERVAL_SECONDS = 0.1
SERVER_READY_TIMEOUT_SECONDS = 15.0
AGENT_READY_TIMEOUT_SECONDS = 30.0
PROCESS_STOP_TIMEOUT_SECONDS = 5.0
MAX_TOKEN_LENGTH = 128


class NativeE2EError(RuntimeError):
    """A bounded, non-sensitive native harness failure."""


def generate_credentials() -> tuple[str, str]:
    """Generate distinct in-memory agent and control credentials."""
    agent_token = secrets.token_urlsafe(48)
    control_token = secrets.token_urlsafe(48)
    if (
        not agent_token
        or not control_token
        or agent_token == control_token
        or len(agent_token) > MAX_TOKEN_LENGTH
        or len(control_token) > MAX_TOKEN_LENGTH
    ):
        raise NativeE2EError("ephemeral credential generation failed")
    return agent_token, control_token


def choose_loopback_port() -> int:
    """Reserve a currently unused loopback port for one native run."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def server_command(port: int) -> list[str]:
    """Return the fixed server argv used by the native harness."""
    _validate_port(port)
    installed_command = os.environ.get("RELAY_E2E_AGENT_RELAY_COMMAND")
    if installed_command:
        return [installed_command, "server"]
    return [
        sys.executable,
        "-m",
        "agent_relay.server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]


def agent_command(port: int, workspace: Path) -> list[str]:
    """Return the fixed agent argv; runtime configuration is environment-only."""
    _validate_port(port)
    if not isinstance(workspace, Path) or not workspace.is_absolute():
        raise ValueError("workspace must be an absolute path")
    installed_command = os.environ.get("RELAY_E2E_AGENT_RELAY_COMMAND")
    if installed_command:
        return [installed_command, "agent"]
    return [sys.executable, "-m", "agent_relay.agent"]


def _validate_port(port: int) -> None:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be a valid TCP port")


def wait_for_process_exit(
    process: subprocess.Popen[Any] | None, *, timeout: float
) -> bool:
    """Wait for one owned process, refusing an unbounded or invalid timeout."""
    if timeout <= 0 or not isinstance(timeout, (int, float)):
        raise ValueError("timeout must be positive")
    if process is None:
        raise ValueError("process is required")
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


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
        if member_group_id == group_id:
            if state != "Z":
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


class NativeLifecycle:
    """Own native resources and preserve a primary scenario failure."""

    def __init__(self) -> None:
        self._cleanups: list[Callable[[], None]] = []
        self.cleanup_error: BaseException | None = None
        self._previous_handlers: dict[signal.Signals, Any] = {}
        self._cleaned = False

    def install_signal_handlers(self) -> None:
        """Turn termination into controlled cleanup instead of orphaning children."""
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

    def own_process(self, process: subprocess.Popen[Any]) -> None:
        try:
            process_group_id = os.getpgid(process.pid)
        except ProcessLookupError:
            process_group_id = process.pid
        self.add_cleanup(
            lambda process=process, process_group_id=process_group_id: terminate_process_group(
                process, process_group_id=process_group_id
            )
        )

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
            except BaseException as error:  # cleanup must not stop remaining cleanup
                failures.append(error)
        for signum, handler in self._previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except BaseException as error:
                failures.append(error)
        self._cleaned = True
        if failures:
            raise NativeE2EError("Linux E2E cleanup failed") from failures[0]

    def __enter__(self) -> NativeLifecycle:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: Any,
    ) -> bool:
        try:
            self.cleanup()
        except BaseException as cleanup_error:
            self.cleanup_error = cleanup_error
            if exception_type is None:
                raise
        return False


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


def _wait_for(
    description: str,
    predicate: Callable[[], bool],
    *,
    timeout: float,
) -> None:
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (ConnectionError, OSError, ValueError) as error:
            if os.environ.get("RELAY_NATIVE_DEBUG") == "1":
                print(
                    f"readiness {description}: {type(error).__name__}",
                    file=sys.stderr,
                )
        time.sleep(POLL_INTERVAL_SECONDS)
    raise NativeE2EError(f"timed out waiting for {description}")


def _status(
    mcp_url: str,
    control_token: str,
    *,
    connected: bool,
    expected_capabilities: tuple[str, ...] | None = None,
    allow_unenrolled: bool = False,
) -> None:
    result = portable_mcp.call_tool(
        mcp_url,
        control_token,
        "relay_device_status",
        {},
        http_timeout=1.0,
        operation_timeout=2.0,
    )
    portable_oracles.validate_status(
        result,
        device_id=None if allow_unenrolled else DEVICE_ID,
        connected=connected,
        expected_capabilities=expected_capabilities,
        allow_unenrolled=allow_unenrolled,
    )


def _server_endpoint_available(mcp_url: str, control_token: str) -> bool:
    """Probe MCP transport availability without exposing response data."""
    try:
        result = portable_mcp.call_tool(
            mcp_url,
            control_token,
            "relay_device_status",
            {},
            http_timeout=1.0,
            operation_timeout=2.0,
        )
    except ConnectionError:
        return False
    return getattr(result, "is_error", getattr(result, "isError", None)) is False


def _runtime(
    *, mcp_url: str, control_token: str, run_id: str, fixtures_root: Path
) -> Any:
    return portable_scenarios.RuntimeConfig(
        mcp_url=mcp_url,
        control_token=control_token,
        device_id=DEVICE_ID,
        run_id=run_id,
        fixture_url=FIXTURE_URL,
        fixtures_root=str(fixtures_root),
    )


def _create_workspace(path: Path) -> None:
    path.mkdir(mode=0o755)
    git = "git"
    subprocess.run(
        [git, "init", "--quiet", "--initial-branch=relay-e2e-marker", str(path)],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        shell=False,
    )
    marker = path / "marker.txt"
    marker.write_text("agent-only workspace\n", encoding="utf-8")
    os.chmod(marker, 0o444)


def _write_artifact(evidence_dir: Path, name: str, payload: bytes) -> None:
    if name not in {"output.log", "success.json"}:
        raise NativeE2EError("unsupported evidence file")
    if evidence_dir.is_symlink():
        raise NativeE2EError("unsafe evidence directory")
    evidence_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    if not all(hasattr(os, name) for name in required_flags):
        raise NativeE2EError("safe evidence writing is unavailable")
    directory_flags = (
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
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


def run_scenario(
    evidence_dir: Path | None = None, *, output_file: Path | None = None
) -> None:
    """Run core MCP, offline detection, and real reconnect on Linux."""
    if os.name != "posix":
        raise NativeE2EError("Linux harness requires POSIX")

    agent_token, control_token = generate_credentials()
    port = choose_loopback_port()
    run_id = f"native-{secrets.token_hex(12)}"
    primary_error: BaseException | None = None
    phase = "setup"
    lifecycle = NativeLifecycle()
    output_lines: list[str] = []
    scenario_phase: list[str] = []

    try:
        lifecycle.install_signal_handlers()
        with lifecycle:
            temporary = tempfile.TemporaryDirectory(
                prefix="agent-relay-native-e2e-"
            )
            lifecycle.add_cleanup(temporary.cleanup)
            root = Path(temporary.name)
            home = root / "home"
            workspace = root / "workspace"
            artifacts = root / "artifacts"
            home.mkdir()
            artifacts.mkdir()
            _create_workspace(workspace)
            repository = Path(__file__).parents[1].resolve()
            mcp_url = f"http://127.0.0.1:{port}/mcp"
            server_environment = _minimal_environment(
                home,
                {
                    "RELAY_SERVER_HOST": "127.0.0.1",
                    "RELAY_SERVER_PORT": str(port),
                    "RELAY_MCP_TOKEN": control_token,
                    "RELAY_AGENT_TOKEN": agent_token,
                },
            )
            agent_environment = _minimal_environment(
                home,
                {
                    "RELAY_URL": f"ws://127.0.0.1:{port}/ws/agent",
                    "RELAY_AGENT_TOKEN": agent_token,
                    "RELAY_AGENT_ID": DEVICE_ID,
                    "RELAY_AGENT_WORKSPACE": str(workspace),
                    "RELAY_AGENT_HEARTBEAT_INTERVAL_SECONDS": "0.2",
                    "RELAY_AGENT_TOOLS": "relay_system_ping,relay_terminal_exec",
                    "RELAY_AGENT_E2E_RUN_ID": run_id,
                },
            )
            runtime = _runtime(
                mcp_url=mcp_url,
                control_token=control_token,
                run_id=run_id,
                fixtures_root=artifacts,
            )
            phase = "server-start"
            server = _spawn(
                server_command(port),
                environment=server_environment,
                cwd=repository,
                lifecycle=lifecycle,
            )
            _wait_for(
                "Linux server",
                lambda: _status(
                    mcp_url, control_token, connected=False, allow_unenrolled=True
                )
                is None,
                timeout=SERVER_READY_TIMEOUT_SECONDS,
            )
            if server.poll() is not None:
                raise NativeE2EError(
                    f"Linux server exited during startup ({server.returncode})"
                )

            phase = "agent-start"
            agent = _spawn(
                agent_command(port, workspace),
                environment=agent_environment,
                cwd=repository,
                lifecycle=lifecycle,
            )
            _wait_for(
                "Linux agent registration",
                lambda: _status(
                    mcp_url,
                    control_token,
                    connected=True,
                    expected_capabilities=CORE_CAPABILITIES,
                )
                is None,
                timeout=AGENT_READY_TIMEOUT_SECONDS,
            )
            if agent.poll() is not None:
                raise NativeE2EError("Linux agent exited after registration")

            phase = "core-scenario"
            portable_scenarios.run_core_scenario(
                runtime,
                scenario_phase,
                expected_capabilities=CORE_CAPABILITIES,
                expected_pwd=str(workspace),
            )

            phase = "agent-stop"
            terminate_process_group(agent)
            phase = "offline-detection"
            _wait_for(
                "Linux agent offline state",
                lambda: _status(mcp_url, control_token, connected=False) is None,
                timeout=AGENT_READY_TIMEOUT_SECONDS,
            )

            phase = "agent-reconnect"
            agent = _spawn(
                agent_command(port, workspace),
                environment=agent_environment,
                cwd=repository,
                lifecycle=lifecycle,
            )
            _wait_for(
                "Linux agent reconnection",
                lambda: _status(
                    mcp_url,
                    control_token,
                    connected=True,
                    expected_capabilities=CORE_CAPABILITIES,
                )
                is None,
                timeout=AGENT_READY_TIMEOUT_SECONDS,
            )
            phase = "reconnected-core-scenario"
            portable_scenarios.run_core_scenario(
                runtime,
                scenario_phase,
                expected_capabilities=CORE_CAPABILITIES,
                expected_pwd=str(workspace),
            )

            phase = "server-stop"
            terminate_process_group(server)
            if server.poll() is None:
                raise NativeE2EError("Linux server did not stop")
            phase = "server-unavailable"
            _wait_for(
                "Linux server unavailable state",
                lambda: not _server_endpoint_available(mcp_url, control_token),
                timeout=SERVER_READY_TIMEOUT_SECONDS,
            )

            phase = "server-restart"
            server = _spawn(
                server_command(port),
                environment=server_environment,
                cwd=repository,
                lifecycle=lifecycle,
            )
            _wait_for(
                "restarted Linux server",
                lambda: _server_endpoint_available(mcp_url, control_token),
                timeout=SERVER_READY_TIMEOUT_SECONDS,
            )
            _wait_for(
                "Linux agent registration after server restart",
                lambda: _status(
                    mcp_url,
                    control_token,
                    connected=True,
                    expected_capabilities=CORE_CAPABILITIES,
                )
                is None,
                timeout=AGENT_READY_TIMEOUT_SECONDS,
            )
            phase = "post-restart-core-scenario"
            portable_scenarios.run_core_scenario(
                runtime,
                scenario_phase,
                expected_capabilities=CORE_CAPABILITIES,
                expected_pwd=str(workspace),
            )
            if server.poll() is not None:
                raise NativeE2EError("Linux server exited after restart")
        if output_file is not None:
            _write_artifact(
                output_file.parent,
                output_file.name,
                b"Linux MCP end-to-end scenario passed.\n",
            )
        if evidence_dir is not None:
            _write_success(evidence_dir)
    except BaseException as error:
        primary_error = error
        if not lifecycle._cleaned:
            try:
                lifecycle.cleanup()
            except BaseException as cleanup_error:
                lifecycle.cleanup_error = cleanup_error
        detail = (
            f": {error}"
            if isinstance(error, NativeE2EError)
            else f": {type(error).__name__}"
        )
        if "scenario_phase" in locals() and scenario_phase:
            detail += f" (phase-{scenario_phase[-1]})"
        failure_line = f"Linux E2E failed at scenario-{phase}{detail}."
        output_lines.append(failure_line)
        print(failure_line, file=sys.stderr)
        if lifecycle.cleanup_error is not None:
            cleanup_line = "Linux E2E cleanup failed."
            output_lines.append(cleanup_line)
            print(cleanup_line, file=sys.stderr)
        if output_file is not None and output_lines:
            try:
                _write_artifact(
                    output_file.parent,
                    output_file.name,
                    ("\n".join(output_lines) + "\n").encode("ascii"),
                )
            except BaseException:
                print("Linux E2E artifact write failed.", file=sys.stderr)

    if primary_error is not None:
        raise primary_error
    print("Linux MCP end-to-end scenario passed.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Linux Agent Relay E2E")
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--output-file", type=Path)
    args = parser.parse_args(argv)
    try:
        run_scenario(args.evidence_dir, output_file=args.output_file)
    except BaseException:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

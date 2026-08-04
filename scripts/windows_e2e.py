#!/usr/bin/env python3
"""Run the minimal native Windows Agent Relay MCP scenario."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import importlib.util
import os
import secrets
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, BinaryIO

try:
    from tests.e2e import mcp_client as portable_mcp
    from tests.e2e import oracles as portable_oracles
    from tests.e2e import scenarios as portable_scenarios
except ModuleNotFoundError as error:
    if error.name not in {"tests", "tests.e2e"}:
        raise

    def _load_portable(name: str) -> Any:
        dotted = f"_agent_relay_windows_e2e_{name}"
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


DEVICE_ID = "windows-native-e2e-agent"
CORE_CAPABILITIES = ("system.ping", "terminal.exec")
POLL_INTERVAL_SECONDS = 0.1
SERVER_READY_TIMEOUT_SECONDS = 15.0
AGENT_READY_TIMEOUT_SECONDS = 30.0
PROCESS_STOP_TIMEOUT_SECONDS = 5.0
MAX_TOKEN_LENGTH = 128
MAX_ARTIFACT_BYTES = 4096
MAX_DIAGNOSTIC_BYTES = 16 * 1024
DIAGNOSTIC_CHUNK_BYTES = 4096

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class WindowsE2EError(RuntimeError):
    """A bounded, non-sensitive Windows harness failure."""


class _LargeInteger(ctypes.Structure):
    _fields_ = [("QuadPart", ctypes.c_longlong)]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", _LargeInteger),
        ("PerJobUserTimeLimit", _LargeInteger),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _BasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", _LargeInteger),
        ("TotalKernelTime", _LargeInteger),
        ("ThisPeriodTotalUserTime", _LargeInteger),
        ("ThisPeriodTotalKernelTime", _LargeInteger),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


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
        raise WindowsE2EError("ephemeral credential generation failed")
    return agent_token, control_token


def choose_loopback_port() -> int:
    """Reserve a currently unused loopback port for one native run."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _windows_system_directory() -> Path:
    """Resolve Windows System32 through the OS, not an inherited PATH."""
    if os.name != "nt":
        raise WindowsE2EError("Windows system directory requires Windows")
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        raise WindowsE2EError("could not resolve Windows system directory")
    return Path(buffer.value)


def _validate_port(port: int) -> None:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be a valid TCP port")


def server_command(port: int) -> list[str]:
    """Return the fixed native Server argv."""
    _validate_port(port)
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
    """Return the fixed Agent argv; runtime configuration is environment-only."""
    _validate_port(port)
    if not isinstance(workspace, Path) or not workspace.is_absolute():
        raise ValueError("workspace must be an absolute path")
    return [sys.executable, "-m", "agent_relay.agent"]


def minimal_environment(home: Path, values: dict[str, str]) -> dict[str, str]:
    """Build a small Windows child environment without unrelated secrets."""
    if not home.is_absolute():
        raise ValueError("home must be absolute")
    environment = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TEMP": str(home),
        "TMP": str(home),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        "PYTHONUNBUFFERED": "1",
    }
    for name in ("SystemRoot", "WINDIR", "PSModulePath"):
        if name in os.environ:
            environment[name] = os.environ[name]
    environment.update(values)
    return environment


class WindowsJob:
    """Own a Windows Job Object with kill-on-close process-tree cleanup."""

    _kernel32: Any
    _handle: int | None

    def __init__(self) -> None:
        if os.name != "nt":
            raise WindowsE2EError("Windows Job Object requires Windows")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_api()
        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise WindowsE2EError("could not create Windows Job Object")
        self._handle: int | None = int(handle)
        try:
            limits = _ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = (
                _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            if not self._kernel32.SetInformationJobObject(
                self._handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise WindowsE2EError("could not configure Windows Job Object")
        except BaseException:
            self.close()
            raise

    def _configure_api(self) -> None:
        kernel32 = self._kernel32
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            wintypes.INT,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            ctypes.c_void_p,
            wintypes.INT,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = wintypes.BOOL

    def assign(self, process: subprocess.Popen[Any]) -> None:
        """Assign a just-started process to this Job Object."""
        if self._handle is None:
            raise WindowsE2EError("Windows Job Object is closed")
        raw_handle = getattr(process, "_handle", None)
        if raw_handle is None:
            raise WindowsE2EError("child process handle is unavailable")
        if not self._kernel32.AssignProcessToJobObject(
            self._handle,
            ctypes.c_void_p(int(raw_handle)),
        ):
            raise WindowsE2EError("could not assign child to Windows Job Object")

    def active_processes(self) -> int:
        """Return the number of active processes assigned to this job."""
        if self._handle is None:
            return 0
        info = _BasicAccountingInformation()
        returned = wintypes.DWORD()
        if not self._kernel32.QueryInformationJobObject(
            self._handle,
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(returned),
        ):
            raise WindowsE2EError("could not inspect Windows Job Object")
        return int(info.ActiveProcesses)

    def terminate(
        self,
        timeout: float = PROCESS_STOP_TIMEOUT_SECONDS,
        processes: Sequence[subprocess.Popen[Any]] | None = None,
    ) -> None:
        """Terminate all assigned descendants, then close the Job Object."""
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be positive")
        if self._handle is None:
            return
        handle = self._handle
        try:
            if not self._kernel32.TerminateJobObject(handle, 1):
                raise WindowsE2EError("could not terminate Windows Job Object")
            deadline = time.monotonic() + timeout
            completed = False
            while time.monotonic() < deadline:
                leaders_stopped = (
                    processes is None
                    or all(process.poll() is not None for process in processes)
                )
                if leaders_stopped and self.active_processes() == 0:
                    completed = True
                    break
                time.sleep(POLL_INTERVAL_SECONDS)
            if not completed:
                raise WindowsE2EError("Windows Job Object cleanup timed out")
        except BaseException as error:
            try:
                self.close()
            except BaseException as close_error:
                raise error from close_error
            raise
        else:
            self.close()

    def close(self) -> None:
        """Close the Job Object; kill-on-close remains the final safety net."""
        if self._handle is None:
            return
        handle = self._handle
        if not self._kernel32.CloseHandle(handle):
            raise WindowsE2EError("could not close Windows Job Object")
        self._handle = None


class WindowsLifecycle:
    """Own Windows resources and preserve a primary scenario failure."""

    def __init__(self, job: WindowsJob | None = None) -> None:
        self.job = job
        self.processes: list[subprocess.Popen[Any]] = []
        self._cleanups: list[Callable[[], None]] = []
        self._cleanup_labels: list[str] = []
        self._diagnostic_threads: list[threading.Thread] = []
        self.cleanup_failures: list[str] = []
        self.cleanup_error: BaseException | None = None
        self._previous_handlers: dict[signal.Signals, Any] = {}
        self._cleaned = False

    def install_signal_handlers(self) -> None:
        """Turn interruption into controlled cleanup instead of orphaning children."""
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

    def add_cleanup(self, cleanup: Callable[[], None], *, label: str | None = None) -> None:
        self._cleanups.append(cleanup)
        self._cleanup_labels.append(label or f"cleanup-{len(self._cleanups)}")

    def track(self, process: subprocess.Popen[Any]) -> None:
        self.processes.append(process)

    def track_diagnostic_thread(self, thread: threading.Thread) -> None:
        self._diagnostic_threads.append(thread)

    def wait_for_diagnostics(self, timeout: float = PROCESS_STOP_TIMEOUT_SECONDS) -> None:
        """Wait for bounded stderr drainers after child processes terminate."""
        deadline = time.monotonic() + timeout
        for thread in self._diagnostic_threads:
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                raise WindowsE2EError("Windows child diagnostics cleanup timed out")

    def close_diagnostic_streams(self) -> None:
        """Close parent stderr handles so drain threads can observe EOF."""
        for process in self.processes:
            stream = process.stderr
            if stream is not None:
                stream.close()

    def stop_process(
        self,
        process: subprocess.Popen[Any],
        timeout: float = PROCESS_STOP_TIMEOUT_SECONDS,
    ) -> None:
        """Stop one process without destroying the shared Server process."""
        if process.poll() is not None:
            return
        if os.name == "nt":
            taskkill = _windows_system_directory() / "taskkill.exe"
            try:
                subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout,
                    shell=False,
                    env={"SystemRoot": str(_windows_system_directory().parent)},
                )
            except subprocess.TimeoutExpired:
                process.kill()
        else:
            process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                raise WindowsE2EError("Windows child cleanup timed out") from error

    def cleanup(self) -> None:
        if self._cleaned:
            return
        failures: list[BaseException] = []
        for signum in self._previous_handlers:
            try:
                signal.signal(signum, signal.SIG_IGN)
            except BaseException as error:
                failures.append(error)
                self.cleanup_failures.append("signal-handlers")
        for index in range(len(self._cleanups) - 1, -1, -1):
            try:
                self._cleanups[index]()
            except BaseException as error:
                failures.append(error)
                self.cleanup_failures.append(self._cleanup_labels[index])
        for signum, handler in self._previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except BaseException as error:
                failures.append(error)
                self.cleanup_failures.append("signal-handlers")
        self._cleaned = True
        if failures:
            raise WindowsE2EError("Windows E2E cleanup failed") from failures[0]

    def __enter__(self) -> WindowsLifecycle:
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


def _spawn(
    argv: Sequence[str],
    *,
    environment: dict[str, str],
    cwd: Path,
    lifecycle: WindowsLifecycle,
    diagnostic_file: Path | None = None,
    new_console: bool = False,
) -> subprocess.Popen[Any]:
    """Start one fixed native child and assign it to the shared Job Object."""
    if os.name != "nt" or lifecycle.job is None:
        raise WindowsE2EError("Windows child spawning requires a Windows Job Object")
    gate_argv = [
        sys.executable,
        "-I",
        "-m",
        "agent_relay._windows_gate",
        *argv,
    ]
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    if new_console:
        creationflags |= getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    process = subprocess.Popen(
        gate_argv,
        cwd=cwd,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE if diagnostic_file is not None else subprocess.DEVNULL,
        creationflags=creationflags,
        shell=False,
    )
    if diagnostic_file is not None:
        if process.stderr is None:
            process.kill()
            process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
            raise WindowsE2EError("Windows child diagnostics pipe is unavailable")
        diagnostic_thread = threading.Thread(
            target=_drain_diagnostic,
            args=(process.stderr, diagnostic_file),
            daemon=True,
        )
        lifecycle.track_diagnostic_thread(diagnostic_thread)
        diagnostic_thread.start()
    try:
        lifecycle.job.assign(process)
    except BaseException:
        process.kill()
        process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
        raise
    lifecycle.track(process)
    try:
        if process.stdin is None:
            raise WindowsE2EError("Windows process gate has no stdin")
        process.stdin.write(b"\x01")
        process.stdin.flush()
        process.stdin.close()
    except BaseException:
        process.kill()
        process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
        raise
    return process


def _drain_diagnostic(stream: BinaryIO, path: Path) -> None:
    """Drain child stderr without allowing disk growth beyond the bound."""
    remaining = MAX_DIAGNOSTIC_BYTES
    try:
        with path.open("wb") as output:
            while True:
                chunk = stream.read(DIAGNOSTIC_CHUNK_BYTES)
                if not chunk:
                    break
                if remaining:
                    kept = chunk[:remaining]
                    output.write(kept)
                    remaining -= len(kept)
    except OSError:
        pass
    finally:
        stream.close()


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
        except (ConnectionError, OSError, ValueError):
            pass
        time.sleep(POLL_INTERVAL_SECONDS)
    raise WindowsE2EError(f"timed out waiting for {description}")


def _diagnostic_category(path: Path) -> str:
    """Classify temporary child diagnostics without exposing their contents."""
    try:
        with path.open("rb") as diagnostic:
            content = diagnostic.read(MAX_DIAGNOSTIC_BYTES + 1).decode(
                "utf-8", errors="replace"
            ).lower()
    except OSError:
        return "diagnostics unavailable"
    for marker, category in (
        ("phase-windows-daemon-spawn", "computer startup windows daemon"),
        ("cua catalog construction failed:", "computer catalog construction"),
        ("computer privacy command failed:", "computer privacy command"),
        ("phase-windows-privacy-skip", "computer startup windows privacy skip"),
        ("phase-privacy-disable", "computer startup privacy disable"),
        ("phase-privacy-reset", "computer startup privacy reset"),
        ("phase-privacy-status", "computer startup privacy status"),
        ("phase-process-spawn", "computer startup process spawn"),
        ("phase-initialize", "computer startup initialize"),
        ("phase-tools-list", "computer startup tools list"),
        ("phase-windows-health", "computer startup windows health"),
        ("phase-session-start", "computer startup session"),
        ("phase-window-select", "computer startup window select"),
        ("phase-capture-readiness", "computer startup capture readiness"),
        ("invalid agent configuration", "invalid agent configuration"),
        ("connection refused", "connection refused"),
        ("cannot connect", "connection failure"),
        ("traceback", "child traceback"),
    ):
        if marker in content:
            return category
    return "child emitted diagnostics" if content.strip() else "no child diagnostics"


def _cleanup_category(_error: BaseException) -> str:
    """Return a closed cleanup diagnostic without exposing exception details."""
    return "cleanup failed"


def _server_endpoint_available(mcp_url: str, control_token: str) -> bool:
    """Probe only MCP transport availability without exposing response data."""
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
    return result.isError is False


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


def _runtime(
    *, mcp_url: str, control_token: str, run_id: str, fixtures_root: Path
) -> Any:
    return portable_scenarios.RuntimeConfig(
        mcp_url=mcp_url,
        control_token=control_token,
        device_id=DEVICE_ID,
        run_id=run_id,
        fixture_url="http://127.0.0.1:8899/",
        fixtures_root=str(fixtures_root),
    )


def _create_workspace(path: Path) -> None:
    path.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=relay-e2e-marker", str(path)],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        shell=False,
    )
    (path / "marker.txt").write_text("agent-only workspace\n", encoding="utf-8")


def _reject_reparse_ancestors(path: Path) -> None:
    """Reject symlinks and Windows reparse points before resolving a write path."""
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    for candidate in (path, *path.parents):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if candidate.is_symlink() or bool(
            getattr(metadata, "st_file_attributes", 0) & reparse_flag
        ):
            raise WindowsE2EError("unsafe evidence path")


def write_artifact(evidence_dir: Path, name: str, payload: bytes) -> None:
    """Write one bounded evidence file without following an existing link."""
    if name not in {"output.log", "success.json"}:
        raise WindowsE2EError("unsupported evidence file")
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise WindowsE2EError("evidence artifact is oversized")
    _reject_reparse_ancestors(evidence_dir)
    evidence_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    _reject_reparse_ancestors(evidence_dir)
    target = evidence_dir / name
    _reject_reparse_ancestors(target)
    if target.exists():
        raise WindowsE2EError("evidence file already exists")
    try:
        with target.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise WindowsE2EError("evidence file already exists") from None


def _write_success(evidence_dir: Path) -> None:
    write_artifact(evidence_dir, "success.json", b'{"status":"passed"}\n')


def run_scenario(
    evidence_dir: Path | None = None, *, output_file: Path | None = None
) -> None:
    """Run core MCP, offline detection, and real reconnect on Windows."""
    if os.name != "nt":
        raise WindowsE2EError("native Windows harness requires Windows")

    agent_token, control_token = generate_credentials()
    port = choose_loopback_port()
    run_id = f"windows-{secrets.token_hex(12)}"
    primary_error: BaseException | None = None
    scenario_error: BaseException | None = None
    phase = "setup"
    lifecycle = WindowsLifecycle()
    output_lines: list[str] = []
    scenario_phase: list[str] = []
    diagnostic_categories: list[tuple[str, str]] = []
    temporary: tempfile.TemporaryDirectory[str] | None = None
    diagnostics: Path | None = None

    try:
        lifecycle.install_signal_handlers()
        temporary = tempfile.TemporaryDirectory(prefix="agent-relay-windows-e2e-")
        lifecycle.add_cleanup(temporary.cleanup)
        root = Path(temporary.name)
        home = root / "home"
        workspace = root / "workspace"
        artifacts = root / "artifacts"
        diagnostics = root / "diagnostics"
        home.mkdir()
        artifacts.mkdir()
        diagnostics.mkdir()
        _create_workspace(workspace)
        lifecycle.job = WindowsJob()

        def collect_diagnostics() -> None:
            for label in ("server", "server-restart", "agent-start", "agent-reconnect"):
                diagnostic_file = diagnostics / f"{label}.stderr.log"
                if diagnostic_file.exists():
                    diagnostic_categories.append(
                        (label, _diagnostic_category(diagnostic_file))
                    )

        lifecycle.add_cleanup(collect_diagnostics)
        lifecycle.add_cleanup(lifecycle.wait_for_diagnostics, label="diagnostics")
        lifecycle.add_cleanup(
            lifecycle.close_diagnostic_streams,
            label="diagnostic-streams",
        )
        lifecycle.add_cleanup(
            lambda: lifecycle.job.terminate(processes=lifecycle.processes),
            label="windows-job",
        )
        repository = Path(__file__).parents[1].resolve()
        mcp_url = f"http://127.0.0.1:{port}/mcp"
        server_environment = minimal_environment(
            home,
            {
                "RELAY_SERVER_HOST": "127.0.0.1",
                "RELAY_SERVER_PORT": str(port),
                "RELAY_MCP_TOKEN": control_token,
                "RELAY_AGENT_TOKEN": agent_token,
                "RELAY_ALLOW_INSECURE_WS": "true",
            },
        )
        agent_environment = minimal_environment(
            home,
            {
                "RELAY_URL": f"ws://127.0.0.1:{port}/ws/agent",
                "RELAY_AGENT_TOKEN": agent_token,
                "RELAY_AGENT_ID": DEVICE_ID,
                "RELAY_AGENT_WORKSPACE": str(workspace),
                "RELAY_ALLOW_INSECURE_WS": "true",
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
            diagnostic_file=diagnostics / "server.stderr.log",
        )
        _wait_for(
            "native Windows server",
            lambda: _status(
                mcp_url, control_token, connected=False, allow_unenrolled=True
            )
            is None,
            timeout=SERVER_READY_TIMEOUT_SECONDS,
        )
        if server.poll() is not None:
            raise WindowsE2EError("native Windows server exited during startup")

        phase = "agent-start"
        agent = _spawn(
            agent_command(port, workspace),
            environment=agent_environment,
            cwd=repository,
            lifecycle=lifecycle,
            diagnostic_file=diagnostics / "agent-start.stderr.log",
        )

        def agent_ready() -> bool:
            if agent.poll() is not None:
                raise WindowsE2EError(
                    f"native Windows agent exited during startup (code {agent.returncode})"
                )
            _status(
                mcp_url,
                control_token,
                connected=True,
                expected_capabilities=CORE_CAPABILITIES,
            )
            return True

        _wait_for(
            "native Windows agent registration",
            agent_ready,
            timeout=AGENT_READY_TIMEOUT_SECONDS,
        )
        if agent.poll() is not None:
            raise WindowsE2EError("native Windows agent exited after registration")
        if lifecycle.job.active_processes() < 2:
            raise WindowsE2EError("Windows Job Object did not retain Server and Agent")

        phase = "core-scenario"
        expected_workspace = str(workspace.resolve(strict=True))
        portable_scenarios.run_core_scenario(
            runtime,
            scenario_phase,
            expected_capabilities=CORE_CAPABILITIES,
            expected_pwd=expected_workspace,
        )

        phase = "agent-stop"
        lifecycle.stop_process(agent)
        phase = "offline-detection"
        _wait_for(
            "native Windows agent offline state",
            lambda: _status(mcp_url, control_token, connected=False) is None,
            timeout=AGENT_READY_TIMEOUT_SECONDS,
        )

        phase = "agent-reconnect"
        agent = _spawn(
            agent_command(port, workspace),
            environment=agent_environment,
            cwd=repository,
            lifecycle=lifecycle,
            diagnostic_file=diagnostics / "agent-reconnect.stderr.log",
        )
        _wait_for(
            "native Windows agent reconnection",
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
            expected_pwd=expected_workspace,
        )

        phase = "server-stop"
        lifecycle.stop_process(server)
        if server.poll() is None:
            raise WindowsE2EError("native Windows server did not stop")
        phase = "server-unavailable"
        _wait_for(
            "native Windows server unavailable state",
            lambda: not _server_endpoint_available(mcp_url, control_token),
            timeout=SERVER_READY_TIMEOUT_SECONDS,
        )

        phase = "server-restart"
        server = _spawn(
            server_command(port),
            environment=server_environment,
            cwd=repository,
            lifecycle=lifecycle,
            diagnostic_file=diagnostics / "server-restart.stderr.log",
        )
        _wait_for(
            "restarted native Windows server",
            lambda: _server_endpoint_available(mcp_url, control_token),
            timeout=SERVER_READY_TIMEOUT_SECONDS,
        )
        _wait_for(
            "native Windows agent registration after server restart",
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
            expected_pwd=expected_workspace,
        )
        if server.poll() is not None:
            raise WindowsE2EError("Windows Job Object lost its owned server")
        if lifecycle.job.active_processes() < 2:
            raise WindowsE2EError("Windows Job Object lost a restarted process")
    except BaseException as error:
        scenario_error = error

    cleanup_error: BaseException | None = None
    if not lifecycle._cleaned:
        try:
            lifecycle.cleanup()
        except BaseException as error:
            cleanup_error = error
            lifecycle.cleanup_error = error
    primary_error = scenario_error or cleanup_error

    for label, category in diagnostic_categories:
        print(
            f"Native Windows E2E {label} diagnostics: {category}.",
            file=sys.stderr,
        )
    if cleanup_error is not None:
        print(
            "Native Windows E2E cleanup: "
            f"{_cleanup_category(cleanup_error)}.",
            file=sys.stderr,
        )

    if primary_error is None:
        try:
            if output_file is not None:
                write_artifact(
                    output_file.parent,
                    output_file.name,
                    b"Native Windows MCP end-to-end scenario passed.\n",
                )
            if evidence_dir is not None:
                _write_success(evidence_dir)
        except BaseException as error:
            primary_error = error

    if primary_error is not None:
        error = primary_error
        detail = (
            f": {error}"
            if isinstance(error, WindowsE2EError)
            else f": {type(error).__name__}"
        )
        if scenario_phase:
            detail += f" (phase-{scenario_phase[-1]})"
        failure_line = f"Native Windows E2E failed at scenario-{phase}{detail}."
        output_lines.append(failure_line)
        print(failure_line, file=sys.stderr)
        if scenario_error is not None and cleanup_error is not None:
            cleanup_line = "Native Windows E2E cleanup failed."
            output_lines.append(cleanup_line)
            print(cleanup_line, file=sys.stderr)
        if output_file is not None and output_lines:
            try:
                write_artifact(
                    output_file.parent,
                    output_file.name,
                    ("\n".join(output_lines) + "\n").encode("ascii"),
                )
            except BaseException:
                print("Native Windows E2E artifact write failed.", file=sys.stderr)

        raise primary_error
    print("Native Windows MCP end-to-end scenario passed.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Native Windows Agent Relay E2E")
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

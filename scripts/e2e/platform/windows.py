"""Windows Job Objects and process primitives for native E2E runs."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, BinaryIO

from ..common import E2EError

POLL_INTERVAL_SECONDS = 0.1
PROCESS_STOP_TIMEOUT_SECONDS = 5.0
MAX_DIAGNOSTIC_BYTES = 16 * 1024
DIAGNOSTIC_CHUNK_BYTES = 4096
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class WindowsE2EError(E2EError):
    """A bounded Windows adapter failure."""


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


def _windows_system_directory() -> Path:
    """Resolve Windows System32 through the OS, not an inherited PATH."""
    if os.name != "nt":
        raise WindowsE2EError("Windows system directory requires Windows")
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        raise WindowsE2EError("could not resolve Windows system directory")
    return Path(buffer.value)


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
        "PYTHONUNBUFFERED": "1",
    }
    environment["PYTHONPATH"] = str(Path(__file__).parents[3] / "src")
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


def _diagnostic_category(path: Path) -> str:
    """Classify temporary child diagnostics without exposing their contents."""
    try:
        with path.open("rb") as diagnostic:
            content = diagnostic.read(MAX_DIAGNOSTIC_BYTES + 1).decode(
                "utf-8", errors="replace"
            ).lower()
    except OSError:
        return "diagnostics unavailable"
    descriptor_match = re.search(
        r"cua provider descriptor failure: category=([a-z-]+)",
        content,
    )
    if descriptor_match:
        return f"computer provider descriptor: {descriptor_match.group(1)}"
    inventory_match = re.search(
        r"cua provider inventory failure: category=([a-z-]+)",
        content,
    )
    if inventory_match:
        return f"computer provider inventory: {inventory_match.group(1)}"
    startup_match = re.search(
        r"computer startup failed: phase=([a-z-]+) category=([a-z-]+)"
        r"(?: driver=([a-z-]+))?",
        content,
    )
    if startup_match:
        driver_hint = (
            f" driver={startup_match.group(3)}"
            if startup_match.group(3)
            else ""
        )
        return (
            "computer startup failure: "
            f"phase={startup_match.group(1)} "
            f"category={startup_match.group(2)}"
            f"{driver_hint}"
        )
    discovery_match = re.search(
        r"cua provider discovery failed: category=([a-z-]+)",
        content,
    )
    if discovery_match:
        return f"computer provider discovery: {discovery_match.group(1)}"
    frame_match = re.search(
        r"server agent frame diagnostic: category=([a-z-]+)",
        content,
    )
    if frame_match:
        return f"server agent frame {frame_match.group(1)}"
    lifecycle_matches = re.findall(r"agent lifecycle phase: ([a-z-]+)", content)
    if lifecycle_matches:
        return f"agent lifecycle {lifecycle_matches[-1]}"
    phase_matches = re.findall(r"computer startup phase: ([a-z-]+)", content)
    if phase_matches:
        phase_categories = {
            "windows-privacy-skip": "computer startup windows privacy skip",
            "windows-daemon-spawn": "computer startup windows daemon spawn",
            "windows-daemon-probe": "computer startup windows daemon probe",
            "windows-daemon-ready": "computer startup windows daemon ready",
            "privacy-environment": "computer startup privacy environment",
            "process-spawn": "computer startup process spawn",
            "initialize": "computer startup initialize",
            "initialize-response": "computer startup initialize response",
            "tools-list": "computer startup tools list",
        }
        return phase_categories.get(phase_matches[-1], "computer startup phase")
    for marker, category in (
        ("computer startup phase: windows-daemon-spawn", "computer startup windows daemon"),
        ("cua catalog construction failed:", "computer catalog construction"),
        ("computer privacy command failed:", "computer privacy command"),
        ("computer startup failed:", "computer startup failure"),
        ("computer startup phase: windows-privacy-skip", "computer startup windows privacy skip"),
        ("computer startup phase: privacy-disable", "computer startup privacy disable"),
        ("computer startup phase: privacy-reset", "computer startup privacy reset"),
        ("computer startup phase: privacy-status", "computer startup privacy status"),
        ("computer startup phase: process-spawn", "computer startup process spawn"),
        ("computer startup phase: initialize", "computer startup initialize"),
        ("computer startup phase: tools-list", "computer startup tools list"),
        ("computer startup phase: windows-health", "computer startup windows health"),
        ("computer startup phase: session-start", "computer startup session"),
        ("computer startup phase: window-select", "computer startup window select"),
        ("computer startup phase: capture-readiness", "computer startup capture readiness"),
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

class WindowsProcessManager:
    """Own Windows children in one kill-on-close Job Object."""

    name = "Windows"
    device_id = "windows-native-e2e-agent"
    run_prefix = "windows"
    cua_run_prefix = "windows-cua"

    def __init__(self) -> None:
        self.job: WindowsJob | None = None
        self.processes: list[subprocess.Popen[Any]] = []
        self._diagnostic_threads: list[threading.Thread] = []
        self._diagnostic_files: list[tuple[str, Path]] = []
        self._previous_handlers: dict[signal.Signals, Any] = {}
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._cleaned = False

    def prepare(self) -> None:
        if os.name != "nt":
            raise WindowsE2EError("Windows harness requires Windows")
        if self.job is not None:
            return

        def interrupted(signum: int, _frame: Any) -> None:
            raise KeyboardInterrupt(f"received signal {signum}")

        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                self._previous_handlers[signum] = signal.signal(signum, interrupted)
            self._temporary = tempfile.TemporaryDirectory(prefix="agent-relay-windows-diagnostics-")
            self.job = WindowsJob()
        except BaseException:
            for signum, handler in self._previous_handlers.items():
                try:
                    signal.signal(signum, handler)
                except BaseException:
                    pass
            self._previous_handlers.clear()
            if self._temporary is not None:
                self._temporary.cleanup()
                self._temporary = None
            raise

    def minimal_environment(self, home: Path, values: dict[str, str]) -> dict[str, str]:
        return minimal_environment(home, values)

    def spawn(self, argv: Sequence[str], *, environment: dict[str, str], cwd: Path, label: str) -> subprocess.Popen[Any]:
        if os.name != "nt" or self.job is None or self._temporary is None:
            raise WindowsE2EError("Windows child spawning requires a Windows Job Object")
        gate_argv = [sys.executable, "-I", "-m", "agent_relay._windows_gate", *argv]
        diagnostic_file = Path(self._temporary.name) / f"{label}.stderr.log"
        process = subprocess.Popen(
            gate_argv,
            cwd=cwd,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            shell=False,
        )
        if process.stderr is None:
            process.kill()
            process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
            raise WindowsE2EError("Windows child diagnostics pipe is unavailable")
        diagnostic_thread = threading.Thread(target=_drain_diagnostic, args=(process.stderr, diagnostic_file), daemon=True)
        self._diagnostic_threads.append(diagnostic_thread)
        self._diagnostic_files.append((label, diagnostic_file))
        diagnostic_thread.start()
        try:
            self.job.assign(process)
            self.processes.append(process)
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

    def stop(self, process: Any) -> None:
        if process.poll() is not None:
            return
        taskkill = _windows_system_directory() / "taskkill.exe"
        try:
            subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=PROCESS_STOP_TIMEOUT_SECONDS,
                shell=False,
                env={"SystemRoot": str(_windows_system_directory().parent)},
            )
        except subprocess.TimeoutExpired:
            process.kill()
        try:
            process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as error:
                raise WindowsE2EError("Windows child cleanup timed out") from error

    def expected_pwd(self, workspace: Path) -> str:
        return str(workspace.resolve(strict=True))

    def cleanup(self) -> None:
        if self._cleaned:
            return
        failures: list[BaseException] = []
        for signum in self._previous_handlers:
            try:
                signal.signal(signum, signal.SIG_IGN)
            except BaseException as error:
                failures.append(error)
        if self.job is not None:
            try:
                self.job.terminate(processes=self.processes)
            except BaseException as error:
                failures.append(error)
        deadline = time.monotonic() + PROCESS_STOP_TIMEOUT_SECONDS
        for thread in self._diagnostic_threads:
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                failures.append(WindowsE2EError("Windows child diagnostics cleanup timed out"))
        for label, path in self._diagnostic_files:
            if path.exists():
                print(f"Windows E2E {label} diagnostics: {_diagnostic_category(path)}.", file=sys.stderr)
        if self._temporary is not None:
            try:
                self._temporary.cleanup()
            except BaseException as error:
                failures.append(error)
        for signum, handler in self._previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except BaseException as error:
                failures.append(error)
        self._cleaned = True
        if failures:
            raise WindowsE2EError("Windows E2E cleanup failed") from failures[0]

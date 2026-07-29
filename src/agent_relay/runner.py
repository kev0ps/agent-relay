"""A terminal runner restricted to a small, locally-defined command table.

The workspace is checked immediately before spawning, but a portable pathname
check cannot completely prevent a privileged concurrent actor replacing it
between that check and ``exec``.  Callers should therefore keep the configured
workspace in a directory not writable by untrusted users.
"""

from __future__ import annotations

import asyncio
import ctypes
import math
import os
import signal
import stat
import subprocess
import sys
from collections.abc import Mapping
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

MAX_TIMEOUT_SECONDS = 3600.0
MAX_OUTPUT_BYTES = 10 * 1024 * 1024
PROCESS_STOP_GRACE_SECONDS = 2.0
PIPE_DRAIN_SECONDS = 2.0


def _trusted_executable(path: str | Path) -> str:
    """Return an absolute, resolved executable path or raise ``ValueError``."""
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ValueError("Configured executable must be an absolute path")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError("Configured executable is not executable")
    return str(resolved)


def _find_system_git() -> str | None:
    """Find Git only in absolute, platform-approved directories."""
    if os.name == "nt":
        directories = (
            Path(r"C:\Program Files\Git\cmd"),
            Path(r"C:\Program Files\Git\bin"),
            Path(r"C:\Program Files (x86)\Git\cmd"),
            Path(r"C:\Program Files (x86)\Git\bin"),
        )
        names = ("git.exe",)
    else:
        directories = tuple(
            Path(entry)
            for entry in os.defpath.split(os.pathsep)
            if entry and Path(entry).is_absolute()
        )
        names = ("git",)
    for directory in directories:
        if not directory.is_absolute():
            continue
        for name in names:
            try:
                return _trusted_executable(directory / name)
            except ValueError:
                continue
    return None


PYTHON_EXECUTABLE = _trusted_executable(sys.executable)
GIT_EXECUTABLE = _find_system_git()

_COMMANDS: dict[str, tuple[str, ...]] = {
    "pwd": (
        PYTHON_EXECUTABLE,
        "-I",
        "-c",
        'import os, sys; sys.stdout.buffer.write((os.getcwd() + "\\n").encode())',
    ),
    "whoami": (
        PYTHON_EXECUTABLE,
        "-I",
        "-c",
        "import getpass; print(getpass.getuser())",
    ),
    "python_version": (PYTHON_EXECUTABLE, "-I", "--version"),
}
if GIT_EXECUTABLE is not None:
    # -c has command-line precedence over local configuration.
    _COMMANDS.update(
        {
            "git_status": (
                GIT_EXECUTABLE,
                "-c",
                "core.fsmonitor=false",
                "status",
                "--short",
            ),
            "git_branch": (
                GIT_EXECUTABLE,
                "-c",
                "core.fsmonitor=false",
                "branch",
                "--show-current",
            ),
        }
    )


def _with_git_executable(
    commands: Mapping[str, tuple[str, ...]], git_executable: str | Path
) -> dict[str, tuple[str, ...]]:
    """Allow local configuration to select Git, without changing its arguments."""
    executable = _trusted_executable(git_executable)
    configured = dict(commands)
    configured["git_status"] = (
        executable,
        "-c",
        "core.fsmonitor=false",
        "status",
        "--short",
    )
    configured["git_branch"] = (
        executable,
        "-c",
        "core.fsmonitor=false",
        "branch",
        "--show-current",
    )
    return configured


@dataclass(frozen=True)
class CommandResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    error: str | None = None


@dataclass
class _PipeCapture:
    data: bytearray
    truncated: bool = False


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name != "nt":
        return False
    # Python 3.11 has no os.path.isjunction.  Reparse points cover junctions.
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _canonical_workspace(workspace: Path | str) -> Path:
    supplied = Path(workspace)
    if not supplied.exists():
        raise ValueError("Configured workspace must be an existing directory")
    if _is_link_or_junction(supplied):
        raise ValueError("Configured workspace must not be a symlink or junction")
    if not supplied.is_dir():
        raise ValueError("Configured workspace must be a directory")
    return supplied.resolve(strict=True)


def _validate_timeout(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
        or value > MAX_TIMEOUT_SECONDS
    ):
        raise ValueError(
            f"timeout_seconds must be finite, > 0 and <= {MAX_TIMEOUT_SECONDS}"
        )
    return float(value)


def _validate_limit(name: str, value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_OUTPUT_BYTES
    ):
        raise ValueError(f"{name} must be an integer from 0 to {MAX_OUTPUT_BYTES}")
    return value


def _validate_commands(
    commands: Mapping[str, tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    validated: dict[str, tuple[str, ...]] = {}
    for command_id, command in commands.items():
        if (
            not isinstance(command_id, str)
            or not command
            or not all(isinstance(part, str) for part in command)
        ):
            raise ValueError("Configured command table is invalid")
        validated[command_id] = (_trusted_executable(command[0]), *command[1:])
    return validated


def _subprocess_group_kwargs() -> dict[str, object]:
    """Return platform process-group options, kept separate for testability."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _git_environment() -> dict[str, str]:
    """Minimal Git environment which ignores system and global configuration."""
    environment = {
        "PATH": os.defpath,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
    }
    if os.name == "nt":
        # Required by Windows process creation, without inheriting arbitrary env.
        environment["SystemRoot"] = str(_windows_system_directory().parent)
    return environment


def _python_environment() -> dict[str, str]:
    """Minimal Python environment, including Windows process requirements."""
    environment = {"PATH": os.defpath}
    if os.name == "nt":
        # Required by Windows process creation, without inheriting arbitrary env.
        environment["SystemRoot"] = str(_windows_system_directory().parent)
        username = os.environ.get("USERNAME")
        if username:
            environment["USERNAME"] = username
    return environment


def _windows_system_directory() -> Path:
    """Return System32 from Windows itself, never from the process environment."""
    if os.name != "nt":
        raise OSError("Windows system directory requested on a non-Windows host")
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        raise OSError("Could not resolve the Windows system directory")
    return Path(buffer.value)


def _trusted_taskkill() -> str:
    """Resolve taskkill.exe using the Windows API and validate the resulting file."""
    return _trusted_executable(_windows_system_directory() / "taskkill.exe")


def _taskkill_environment() -> dict[str, str]:
    """The minimal Windows environment needed to create taskkill.exe."""
    return {"SystemRoot": str(_windows_system_directory().parent)}


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
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


class _WindowsJob:
    """A kill-on-close Windows Job Object bound to one subprocess tree."""

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    def __init__(self, pid: int) -> None:
        if os.name != "nt":
            raise OSError("Windows Job Objects are unavailable")
        self._handle: wintypes.HANDLE | None = None
        kernel32: object | None = None
        try:
            kernel32 = ctypes.windll.kernel32
            self._configure_api(kernel32)
            self._handle = kernel32.CreateJobObjectW(None, None)
            if not self._handle:
                raise OSError("Could not create Windows Job Object")
            information = _ExtendedLimitInformation()
            information.BasicLimitInformation.LimitFlags = (
                self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            if not kernel32.SetInformationJobObject(
                self._handle,
                self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(information),
                ctypes.sizeof(_ExtendedLimitInformation),
            ):
                raise OSError("Could not configure Windows Job Object")
            process = kernel32.OpenProcess(
                self._PROCESS_SET_QUOTA | self._PROCESS_TERMINATE, False, pid
            )
            if not process:
                raise OSError("Could not open subprocess for Windows Job Object")
            try:
                if not kernel32.AssignProcessToJobObject(self._handle, process):
                    raise OSError("Could not assign subprocess to Windows Job Object")
            finally:
                kernel32.CloseHandle(process)
        except Exception as exc:
            if self._handle is not None and kernel32 is not None:
                try:
                    kernel32.CloseHandle(self._handle)
                except Exception:
                    pass
            self._handle = None
            if isinstance(exc, OSError):
                raise
            raise OSError("Could not initialize Windows Job Object") from exc

    @staticmethod
    def _configure_api(kernel32: object) -> None:
        """Declare the Win32 ABI before making any Job Object calls."""
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE, wintypes.HANDLE
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

    def terminate(self) -> None:
        """Terminate every process currently assigned to this Job Object."""
        if self._handle is None:
            return
        try:
            kernel32 = getattr(getattr(ctypes, "windll"), "kernel32")
            terminated = getattr(kernel32, "TerminateJobObject")(self._handle, 1)
        except Exception as exc:
            raise OSError("Could not terminate Windows Job Object") from exc
        if not terminated:
            raise OSError("Could not terminate Windows Job Object")

    def close(self) -> None:
        if self._handle is not None:
            try:
                closed = ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception as exc:
                raise OSError("Could not close Windows Job Object") from exc
            if not closed:
                raise OSError("Could not close Windows Job Object")
            self._handle = None


class CommandRunner:
    """Run only commands from the module's fixed command table in one workspace."""

    def __init__(
        self,
        workspace: Path | str,
        *,
        timeout_seconds: float = 30.0,
        stdout_limit: int = 64 * 1024,
        stderr_limit: int = 64 * 1024,
        git_executable: str | Path | None = None,
    ) -> None:
        self.workspace = _canonical_workspace(workspace)
        self.timeout_seconds = _validate_timeout(timeout_seconds)
        self.stdout_limit = _validate_limit("stdout_limit", stdout_limit)
        self.stderr_limit = _validate_limit("stderr_limit", stderr_limit)
        commands = (
            _with_git_executable(_COMMANDS, git_executable)
            if git_executable is not None
            else _COMMANDS
        )
        self._commands = _validate_commands(commands)

    async def run(self, command_id: str) -> CommandResult:
        command = (
            self._commands.get(command_id) if isinstance(command_id, str) else None
        )
        if command is None:
            return CommandResult(error=f"Unknown command_id: {command_id}")
        if not self.workspace.is_dir() or _is_link_or_junction(self.workspace):
            return CommandResult(
                error="Configured workspace is not an existing directory"
            )

        environment = (
            _git_environment()
            if command_id in {"git_status", "git_branch"}
            else (
                _python_environment()
                if command_id in {"pwd", "whoami", "python_version"}
                else None
            )
        )
        job: _WindowsJob | None = None
        if os.name == "nt":
            started = await self._start_windows_gate(command, environment)
            if started is None:
                return CommandResult(
                    error="Could not guarantee Windows process cleanup"
                )
            process, job = started
        else:
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=self.workspace,
                    env=environment,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **_subprocess_group_kwargs(),
                )
            except OSError:
                return CommandResult(error="Could not start configured command")

        stdout_capture = _PipeCapture(bytearray())
        stderr_capture = _PipeCapture(bytearray())
        stdout_task = asyncio.create_task(
            self._read_limited(process.stdout, self.stdout_limit, stdout_capture)
        )
        stderr_task = asyncio.create_task(
            self._read_limited(process.stderr, self.stderr_limit, stderr_capture)
        )
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=self.timeout_seconds)
        except TimeoutError:
            timed_out = True
            if not await self._stop_process_tree(process, job):
                await self._drain_pipes(stdout_task, stderr_task)
                return CommandResult(error="Could not guarantee process cleanup")
        except asyncio.CancelledError:
            await self._stop_process_tree(process, job)
            await self._drain_pipes(stdout_task, stderr_task)
            if job is not None:
                job.close()
            raise

        await self._drain_pipes(stdout_task, stderr_task)
        if job is not None:
            try:
                job.close()
            except OSError:
                return CommandResult(
                    error="Could not guarantee Windows process cleanup"
                )
        return CommandResult(
            stdout=bytes(stdout_capture.data).decode("utf-8", errors="replace"),
            stderr=bytes(stderr_capture.data).decode("utf-8", errors="replace"),
            exit_code=process.returncode,
            timed_out=timed_out,
            stdout_truncated=stdout_capture.truncated,
            stderr_truncated=stderr_capture.truncated,
        )

    async def _start_windows_gate(
        self, command: tuple[str, ...], environment: dict[str, str] | None
    ) -> tuple[asyncio.subprocess.Process, _WindowsJob] | None:
        """Start a gate, attach it to a Job, then permit its fixed command to run."""
        try:
            process = await asyncio.create_subprocess_exec(
                PYTHON_EXECUTABLE,
                "-I",
                "-m",
                "agent_relay._windows_gate",
                *command,
                cwd=self.workspace,
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_subprocess_group_kwargs(),
            )
        except OSError:
            return None
        try:
            job = _WindowsJob(process.pid)
        except OSError:
            await self._stop_unreleased_gate(process)
            return None
        try:
            if process.stdin is None:
                raise OSError("Windows gate has no stdin")
            process.stdin.write(b"\x01")
            await process.stdin.drain()
            process.stdin.close()
        except asyncio.CancelledError:
            try:
                job.close()
            except OSError:
                pass
            await self._stop_unreleased_gate(process)
            raise
        except Exception:
            try:
                job.close()
            except OSError:
                pass
            await self._stop_unreleased_gate(process)
            return None
        return process, job

    @staticmethod
    async def _stop_unreleased_gate(process: asyncio.subprocess.Process) -> None:
        """Stop a gate which has not received permission to start the command."""
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), PROCESS_STOP_GRACE_SECONDS)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def _stop_process_tree(
        self, process: asyncio.subprocess.Process, job: _WindowsJob | None = None
    ) -> bool:
        if process.returncode is not None:
            return True
        if os.name == "nt":
            if job is not None and hasattr(job, "terminate"):
                try:
                    job.terminate()
                except OSError:
                    await self._taskkill_tree(process)
            else:
                await self._taskkill_tree(process)
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=PROCESS_STOP_GRACE_SECONDS)
            return True
        except TimeoutError:
            pass
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=PROCESS_STOP_GRACE_SECONDS)
        except TimeoutError:
            if job is None:
                return False
            try:
                job.close()
            except OSError:
                return False
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=PROCESS_STOP_GRACE_SECONDS
                )
            except TimeoutError:
                return False
        return True

    async def _taskkill_tree(self, process: asyncio.subprocess.Process) -> None:
        killer: asyncio.subprocess.Process | None = None
        try:
            killer = await asyncio.create_subprocess_exec(
                _trusted_taskkill(),
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                env=_taskkill_environment(),
            )
            await asyncio.wait_for(killer.wait(), timeout=PROCESS_STOP_GRACE_SECONDS)
            if killer.returncode == 0:
                return
        except (OSError, TimeoutError):
            pass
        if killer is not None and killer.returncode is None:
            killer.kill()
            await killer.wait()
        process.terminate()

    @staticmethod
    async def _drain_pipes(*tasks: asyncio.Task[None]) -> None:
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=PIPE_DRAIN_SECONDS)
        except TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    async def _read_limited(
        stream: asyncio.StreamReader | None, limit: int, capture: _PipeCapture
    ) -> None:
        if stream is None:
            return
        while chunk := await stream.read(4096):
            remaining = max(limit - len(capture.data), 0)
            capture.data.extend(chunk[:remaining])
            capture.truncated |= len(chunk) > remaining

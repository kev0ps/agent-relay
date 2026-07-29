from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

import agent_relay.runner as runner
from agent_relay.runner import CommandResult, CommandRunner


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def install_test_commands(
    monkeypatch: pytest.MonkeyPatch, **entries: tuple[str, ...]
) -> None:
    monkeypatch.setattr(runner, "_COMMANDS", entries)


def test_command_table_cannot_be_replaced_through_public_api(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="commands"):
        CommandRunner(tmp_path, commands={"arbitrary": (sys.executable, "-V")})  # type: ignore[call-arg]


def test_runs_an_allowed_command_in_workspace(tmp_path: Path) -> None:
    result = run(CommandRunner(tmp_path).run("pwd"))

    assert isinstance(result, CommandResult)
    assert result.exit_code == 0
    assert result.stdout.strip() == str(tmp_path.resolve())
    assert not result.timed_out


def test_pwd_command_writes_a_stable_lf_line_ending() -> None:
    command = runner._COMMANDS["pwd"]

    assert "sys.stdout.buffer.write" in command[-1]
    assert '\\n' in command[-1]


def test_whoami_does_not_import_workspace_getpass_module(tmp_path: Path) -> None:
    marker = tmp_path / "workspace-module-executed"
    (tmp_path / "getpass.py").write_text(
        f"import pathlib, sys; pathlib.Path({str(marker)!r}).write_text('executed'); "
        "sys.exit(23)"
    )

    result = run(CommandRunner(tmp_path).run("whoami"))

    assert result.exit_code == 0
    assert not marker.exists()


@pytest.mark.parametrize("command_id", ["pwd", "whoami", "python_version"])
def test_python_commands_ignore_pythonpath_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_id: str
) -> None:
    injection_dir = tmp_path / "injection"
    injection_dir.mkdir()
    marker = tmp_path / "pythonpath-module-executed"
    (injection_dir / "sitecustomize.py").write_text(
        f"import pathlib, sys; pathlib.Path({str(marker)!r}).write_text('executed'); "
        "sys.exit(23)"
    )
    monkeypatch.setenv("PYTHONPATH", str(injection_dir))

    result = run(CommandRunner(tmp_path).run(command_id))

    assert result.exit_code == 0
    assert not marker.exists()


def test_unknown_command_is_rejected_before_process_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    async def should_not_run(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("a subprocess must not be created")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", should_not_run)

    result = run(CommandRunner(tmp_path).run("not_allowed"))

    assert result.error == "Unknown command_id: not_allowed"
    assert result.exit_code is None
    assert not called


def test_missing_workspace_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="existing directory"):
        CommandRunner(tmp_path / "missing")


def test_file_and_symlink_workspaces_are_rejected(tmp_path: Path) -> None:
    file = tmp_path / "file"
    file.write_text("no")
    with pytest.raises(ValueError, match="directory"):
        CommandRunner(file)

    link = tmp_path / "link"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")
    with pytest.raises(ValueError, match="symlink"):
        CommandRunner(link)


def test_timeout_terminates_process_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "survived"
    child = (
        "import pathlib, sys, time; time.sleep(.4); "
        "pathlib.Path(sys.argv[1]).write_text('survived')"
    )
    parent = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); "
        "time.sleep(10)"
    )
    install_test_commands(
        monkeypatch, tree=(sys.executable, "-c", parent, child, str(marker))
    )

    result = run(CommandRunner(tmp_path, timeout_seconds=0.05).run("tree"))
    assert result.timed_out
    run(asyncio.sleep(0.6))
    assert not marker.exists()


def test_cancellation_terminates_process_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "survived"
    child = (
        "import pathlib, sys, time; time.sleep(.4); "
        "pathlib.Path(sys.argv[1]).write_text('survived')"
    )
    parent = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); "
        "time.sleep(10)"
    )
    install_test_commands(
        monkeypatch, tree=(sys.executable, "-c", parent, child, str(marker))
    )

    async def cancel() -> None:
        task = asyncio.create_task(CommandRunner(tmp_path).run("tree"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(cancel())
    run(asyncio.sleep(0.6))
    assert not marker.exists()


def test_stdout_and_stderr_are_drained_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = "import sys; sys.stdout.write('x'*1000000); sys.stderr.write('y'*1000000)"
    install_test_commands(monkeypatch, loud=(sys.executable, "-c", code))

    result = run(
        CommandRunner(tmp_path, stdout_limit=20, stderr_limit=20).run("loud")
    )
    assert result.exit_code == 0
    assert result.stdout_truncated and result.stderr_truncated
    assert len(result.stdout.encode()) == 20
    assert len(result.stderr.encode()) == 20


def test_stdout_limit_does_not_truncate_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = "import sys; sys.stdout.write('x'*100); sys.stderr.write('error')"
    install_test_commands(monkeypatch, output=(sys.executable, "-c", code))

    result = run(
        CommandRunner(tmp_path, stdout_limit=20, stderr_limit=20).run("output")
    )

    assert result.stdout_truncated
    assert not result.stderr_truncated
    assert len(result.stdout.encode()) == 20
    assert result.stderr == "error"


def test_stderr_limit_does_not_truncate_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = "import sys; sys.stdout.write('output'); sys.stderr.write('y'*100)"
    install_test_commands(monkeypatch, output=(sys.executable, "-c", code))

    result = run(
        CommandRunner(tmp_path, stdout_limit=20, stderr_limit=20).run("output")
    )

    assert not result.stdout_truncated
    assert result.stderr_truncated
    assert result.stdout == "output"
    assert len(result.stderr.encode()) == 20


def test_utf8_truncation_uses_replacement_character(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_test_commands(
        monkeypatch,
        utf8=(
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'\\xe2\\x82\\xac')",
        )
    )
    result = run(CommandRunner(tmp_path, stdout_limit=2).run("utf8"))
    assert result.stdout == "�"
    assert result.stdout_truncated


@pytest.mark.parametrize(
    "value", [True, False, 0, -1, float("inf"), float("nan"), 3601]
)
def test_invalid_timeouts_are_rejected(tmp_path: Path, value: object) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        CommandRunner(tmp_path, timeout_seconds=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [True, -1, 1.2, 10 * 1024 * 1024 + 1])
def test_invalid_output_limits_are_rejected(tmp_path: Path, value: object) -> None:
    with pytest.raises(ValueError, match="stdout_limit"):
        CommandRunner(tmp_path, stdout_limit=value)  # type: ignore[arg-type]


def test_remote_arguments_cannot_be_passed_to_command(tmp_path: Path) -> None:
    result = run(CommandRunner(tmp_path).run("pwd --not-an-argument"))
    assert result.error == "Unknown command_id: pwd --not-an-argument"


def test_non_zero_exit_code_is_returned_from_test_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_test_commands(
        monkeypatch, fail=(sys.executable, "-c", "import sys; sys.exit(7)")
    )
    result = run(CommandRunner(tmp_path).run("fail"))
    assert result.exit_code == 7


def test_fake_git_on_path_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_dir = tmp_path / "fake-bin"
    fake_dir.mkdir()
    fake_git = fake_dir / "git"
    fake_git.write_text("#!/bin/sh\necho fake-git >&2\nexit 42\n")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_dir))

    result = run(CommandRunner(tmp_path).run("git_status"))
    assert result.exit_code != 42
    assert "fake-git" not in result.stderr


def test_git_search_skips_relative_default_path_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approved = tmp_path / "approved"
    approved.mkdir()
    git = approved / "git"
    git.write_text("#!/bin/sh\nexit 0\n")
    git.chmod(0o755)
    monkeypatch.setattr(runner.os, "defpath", f".:relative:{approved}")

    assert runner._find_system_git() == str(git.resolve())


def test_explicit_git_path_must_be_absolute_and_only_changes_git_binary(
    tmp_path: Path,
) -> None:
    git = tmp_path / "git"
    git.write_text("#!/bin/sh\nexit 0\n")
    git.chmod(0o755)

    configured = CommandRunner(tmp_path, git_executable=git)

    assert configured._commands["git_status"] == (
        str(git.resolve()),
        "-c",
        "core.fsmonitor=false",
        "status",
        "--short",
    )
    with pytest.raises(ValueError, match="absolute"):
        CommandRunner(tmp_path, git_executable="git")


def test_taskkill_is_trusted_and_uses_minimal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system32 = tmp_path / "System32"
    system32.mkdir()
    taskkill = system32 / "taskkill.exe"
    taskkill.write_text("")
    taskkill.chmod(0o755)
    monkeypatch.setattr(runner, "_windows_system_directory", lambda: system32)
    monkeypatch.setenv("SystemRoot", r"C:\\attacker")

    assert runner._trusted_taskkill() == str(taskkill.resolve())
    assert runner._taskkill_environment() == {"SystemRoot": str(tmp_path)}


def test_taskkill_nonzero_or_timeout_falls_back_to_process_terminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Process:
        pid = 123
        returncode = None
        terminated = False

        def terminate(self) -> None:
            self.terminated = True

    class Killer:
        def __init__(self, *, returncode: int | None, timeout: bool) -> None:
            self.returncode = returncode
            self.timeout = timeout

        async def wait(self) -> None:
            if self.timeout:
                await asyncio.sleep(10)

        def kill(self) -> None:
            self.returncode = -9

    for killer in (
        Killer(returncode=1, timeout=False),
        Killer(returncode=None, timeout=True),
    ):
        process = Process()

        async def create(*args: object, **kwargs: object) -> Killer:
            return killer

        monkeypatch.setattr(
            runner,
            "_trusted_taskkill",
            lambda: "C:\\Windows\\System32\\taskkill.exe",
        )
        monkeypatch.setattr(runner, "_taskkill_environment", lambda: {})
        monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
        run(CommandRunner(tmp_path)._taskkill_tree(process))  # type: ignore[arg-type]
        assert process.terminated


def test_windows_gate_is_not_released_when_job_assignment_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Stdin:
        def __init__(self) -> None:
            self.writes: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.writes.append(data)

        async def drain(self) -> None:
            return None

    class Process:
        pid = 123
        returncode = None
        stdin = Stdin()
        terminated = False

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> None:
            return None

    process = Process()

    async def create(*args: object, **kwargs: object) -> Process:
        return process

    class FailingJob:
        def __init__(self, pid: int) -> None:
            raise OSError("assignment failed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(runner, "_WindowsJob", FailingJob)

    assert run(CommandRunner(tmp_path)._start_windows_gate(("fixed",), {})) is None
    assert process.stdin.writes == []
    assert process.terminated


def test_windows_gate_is_released_only_after_job_assignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    class Stdin:
        def write(self, data: bytes) -> None:
            assert data == b"\x01"
            events.append("released")

        async def drain(self) -> None:
            events.append("drained")

        def close(self) -> None:
            events.append("closed")

    class Process:
        pid = 123
        stdin = Stdin()

    async def create(*args: object, **kwargs: object) -> Process:
        events.append("created")
        return Process()

    class Job:
        def __init__(self, pid: int) -> None:
            events.append("assigned")

        def close(self) -> None:
            return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(runner, "_WindowsJob", Job)

    started = run(CommandRunner(tmp_path)._start_windows_gate(("fixed",), {}))
    assert started is not None
    assert events == ["created", "assigned", "released", "drained", "closed"]


def test_windows_job_uses_ctypes_signatures_and_structure_pointers(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Api:
        def __init__(self, name: str, result: object) -> None:
            self.name = name
            self.result = result
            self.argtypes: object = None
            self.restype: object = None

        def __call__(self, *args: object) -> object:
            calls.append((self.name, args))
            return self.result

    class Kernel32:
        CreateJobObjectW = Api("CreateJobObjectW", 10)
        SetInformationJobObject = Api("SetInformationJobObject", True)
        OpenProcess = Api("OpenProcess", 11)
        AssignProcessToJobObject = Api("AssignProcessToJobObject", True)
        TerminateJobObject = Api("TerminateJobObject", True)
        CloseHandle = Api("CloseHandle", True)

    class Windll:
        kernel32 = Kernel32()

    monkeypatch.setattr(runner.os, "name", "nt")
    monkeypatch.setattr(runner.ctypes, "windll", Windll(), raising=False)
    job = runner._WindowsJob(42)

    assert Kernel32.CreateJobObjectW.argtypes is not None
    assert Kernel32.SetInformationJobObject.argtypes is not None
    assert Kernel32.OpenProcess.argtypes is not None
    assert Kernel32.AssignProcessToJobObject.argtypes is not None
    assert Kernel32.TerminateJobObject.argtypes is not None
    assert Kernel32.CloseHandle.argtypes is not None
    info_call = next(args for name, args in calls if name == "SetInformationJobObject")
    assert type(info_call[2]).__name__ == "CArgObject"
    job.close()


def test_windows_job_terminates_all_assigned_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Api:
        argtypes: object = None
        restype: object = None

        def __call__(self, handle: object, exit_code: int) -> bool:
            assert handle == 10
            assert exit_code == 1
            return True

    class Kernel32:
        TerminateJobObject = Api()

    class Windll:
        kernel32 = Kernel32()

    monkeypatch.setattr(runner.ctypes, "windll", Windll(), raising=False)
    job = object.__new__(runner._WindowsJob)
    setattr(job, "_handle", 10)
    job.terminate()


def test_windows_job_is_closed_after_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Process:
        returncode: int | None = None
        stdout = None
        stderr = None

        async def wait(self) -> None:
            await asyncio.sleep(10)

    class Job:
        closed = False

        def close(self) -> None:
            self.closed = True

    process = Process()
    job = Job()
    command_runner = CommandRunner(tmp_path, timeout_seconds=0.01)

    async def start(
        self: CommandRunner, command: tuple[str, ...], environment: object
    ) -> tuple[Process, Job]:
        return process, job

    async def stop(self: CommandRunner, process: Process, job: Job) -> bool:
        process.returncode = -15
        return True

    monkeypatch.setattr(runner.os, "name", "nt")
    monkeypatch.setattr(runner, "_python_environment", lambda: {})
    monkeypatch.setattr(CommandRunner, "_start_windows_gate", start)
    monkeypatch.setattr(CommandRunner, "_stop_process_tree", stop)

    result = run(command_runner.run("pwd"))
    assert result.timed_out
    assert job.closed


def test_windows_job_is_closed_after_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Process:
        returncode: int | None = None
        stdout = None
        stderr = None

        async def wait(self) -> None:
            await asyncio.sleep(10)

    class Job:
        closed = False

        def close(self) -> None:
            self.closed = True

    process = Process()
    job = Job()
    command_runner = CommandRunner(tmp_path)

    async def start(
        self: CommandRunner, command: tuple[str, ...], environment: object
    ) -> tuple[Process, Job]:
        return process, job

    async def stop(self: CommandRunner, process: Process, job: Job) -> bool:
        process.returncode = -15
        return True

    monkeypatch.setattr(runner.os, "name", "nt")
    monkeypatch.setattr(runner, "_python_environment", lambda: {})
    monkeypatch.setattr(CommandRunner, "_start_windows_gate", start)
    monkeypatch.setattr(CommandRunner, "_stop_process_tree", stop)

    async def cancel() -> None:
        task = asyncio.create_task(command_runner.run("pwd"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(cancel())
    assert job.closed


@pytest.mark.skipif(os.name != "nt", reason="native Windows Job Object test")
def test_windows_gate_runs_a_very_short_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "short-command-ran"
    install_test_commands(
        monkeypatch,
        short=(
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ok')",
        ),
    )

    result = run(CommandRunner(tmp_path).run("short"))
    assert result.exit_code == 0
    assert marker.read_text() == "ok"


@pytest.mark.skipif(os.name != "nt", reason="native Windows Job Object test")
def test_windows_gate_kills_an_immediate_child_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "child-survived"
    child = (
        "from pathlib import Path; import sys, time; time.sleep(.4); "
        "Path(sys.argv[1]).write_text('escaped')"
    )
    parent = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); "
        "time.sleep(10)"
    )
    install_test_commands(
        monkeypatch, immediate_child=(sys.executable, "-c", parent, child, str(marker))
    )

    result = run(CommandRunner(tmp_path, timeout_seconds=0.05).run("immediate_child"))
    assert result.timed_out
    run(asyncio.sleep(0.6))
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX shell fsmonitor command")
def test_git_local_fsmonitor_is_neutralized(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "core.fsmonitor", str(hook)], check=True
    )

    result = run(CommandRunner(tmp_path).run("git_status"))
    assert result.exit_code == 0
    assert not marker.exists()

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_relay import cli


class _Stdin:
    def __init__(self, interactive: bool) -> None:
        self.interactive = interactive

    def isatty(self) -> bool:
        return self.interactive


def _setup_fake_uv(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
) -> list[list[str]]:
    commands: list[list[str]] = []
    monkeypatch.setattr(cli.uninstall, "_is_windows", lambda: False)
    monkeypatch.setattr(cli.uninstall, "find_uv", lambda: Path("uv"))

    def run(command: list[str], *, check: bool) -> SimpleNamespace:
        assert check is False
        commands.append(command)
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(cli.uninstall.subprocess, "run", run)
    return commands


def _default_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    data_dir = tmp_path / ".agent-relay"
    monkeypatch.setattr(cli.config, "DEFAULT_CONFIG_PATH", data_dir / "config.yaml")
    return data_dir


def test_help_lists_uninstall(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--help"]) == 0
    assert "uninstall [--purge] [--yes]" in capsys.readouterr().out


def test_yes_requires_purge(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["uninstall", "--yes"]) == 1
    assert "--yes is only valid with --purge" in capsys.readouterr().err


def test_uninstall_delegates_to_uv_and_preserves_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _default_data_dir(monkeypatch, tmp_path)
    data_dir.mkdir()
    marker = data_dir / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    commands = _setup_fake_uv(monkeypatch)

    assert cli.main(["uninstall"]) == 0

    assert commands == [["uv", "tool", "uninstall", "agent-relay"]]
    assert marker.read_text(encoding="utf-8") == "keep"
    assert "preserved Agent Relay data" in capsys.readouterr().out


def test_uv_failure_preserves_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _default_data_dir(monkeypatch, tmp_path)
    data_dir.mkdir()
    marker = data_dir / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    _setup_fake_uv(monkeypatch, returncode=3)

    assert cli.main(["uninstall"]) == 1

    assert marker.exists()
    assert "exit code 3" in capsys.readouterr().err


def test_missing_uv_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli.uninstall, "find_uv", lambda: None)

    assert cli.main(["uninstall"]) == 1
    assert "uv was not found" in capsys.readouterr().err


def test_purge_with_yes_removes_default_data_after_uv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _default_data_dir(monkeypatch, tmp_path)
    data_dir.mkdir()
    (data_dir / "secrets").mkdir()
    (data_dir / "secrets" / "token").write_text("secret", encoding="utf-8")
    commands = _setup_fake_uv(monkeypatch)

    assert cli.main(["uninstall", "--purge", "--yes"]) == 0

    assert commands == [["uv", "tool", "uninstall", "agent-relay"]]
    assert not data_dir.exists()
    assert "removed Agent Relay data" in capsys.readouterr().out


def test_interactive_purge_can_be_cancelled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _default_data_dir(monkeypatch, tmp_path)
    data_dir.mkdir()
    (data_dir / "keep.txt").write_text("keep", encoding="utf-8")
    commands = _setup_fake_uv(monkeypatch)
    monkeypatch.setattr(cli.sys, "stdin", _Stdin(True))
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    assert cli.main(["uninstall", "--purge"]) == 0

    assert commands == []
    assert data_dir.exists()
    assert "uninstall cancelled" in capsys.readouterr().out


def test_noninteractive_purge_requires_yes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _default_data_dir(monkeypatch, tmp_path)
    data_dir.mkdir()
    commands = _setup_fake_uv(monkeypatch)
    monkeypatch.setattr(cli.sys, "stdin", _Stdin(False))

    assert cli.main(["uninstall", "--purge"]) == 1

    assert commands == []
    assert data_dir.exists()
    assert "requires --yes" in capsys.readouterr().err


def test_purge_refuses_symbolic_link_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _default_data_dir(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, data_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable in this test environment")
    commands = _setup_fake_uv(monkeypatch)

    assert cli.main(["uninstall", "--purge", "--yes"]) == 1

    assert commands == []
    assert outside.exists()
    assert "symbolic-link" in capsys.readouterr().err


def test_custom_config_is_preserved_during_default_purge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _default_data_dir(monkeypatch, tmp_path)
    data_dir.mkdir()
    custom_config = tmp_path / "custom" / "config.yaml"
    custom_config.parent.mkdir()
    custom_config.write_text("custom", encoding="utf-8")
    _setup_fake_uv(monkeypatch)

    assert cli.main(
        ["--config", str(custom_config), "uninstall", "--purge", "--yes"]
    ) == 0

    assert not data_dir.exists()
    assert custom_config.exists()
    assert "preserving custom configuration" in capsys.readouterr().out


def test_uv_subprocess_errors_are_converted_to_cli_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli.uninstall, "_is_windows", lambda: False)
    monkeypatch.setattr(cli.uninstall, "find_uv", lambda: Path("uv"))

    def run(_command: list[str], *, check: bool) -> SimpleNamespace:
        assert check is False
        raise PermissionError("not exposed to the user")

    monkeypatch.setattr(cli.uninstall.subprocess, "run", run)

    assert cli.main(["uninstall"]) == 1
    captured = capsys.readouterr()
    assert "could not start uv" in captured.err
    assert "not exposed" not in captured.err


def test_windows_uninstall_delegates_until_the_cli_exits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_path = tmp_path / "uninstall.log"
    fd = os.open(log_path, os.O_CREAT | os.O_WRONLY, 0o600)
    launches: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(cli.uninstall, "find_uv", lambda: Path("C:/uv/uv.exe"))
    monkeypatch.setattr(cli.uninstall, "_is_windows", lambda: True)
    monkeypatch.setattr(
        cli.uninstall, "_find_powershell", lambda: Path("C:/Windows/powershell.exe")
    )
    monkeypatch.setattr(
        cli.uninstall.tempfile,
        "mkstemp",
        lambda **_: (fd, str(log_path)),
    )

    def popen(command: list[str], **kwargs: object) -> SimpleNamespace:
        launches.append((command, kwargs))
        return SimpleNamespace()

    monkeypatch.setattr(cli.uninstall.subprocess, "Popen", popen)

    assert cli.main(["uninstall"]) == 0
    output = capsys.readouterr().out
    assert "scheduled Agent Relay uninstallation" in output
    assert len(launches) == 1
    assert "-EncodedCommand" in launches[0][0]
    assert "tool uninstall agent-relay" not in " ".join(launches[0][0])
    log_path.unlink(missing_ok=True)


def test_windows_uninstall_script_has_bounded_retry_and_final_status(
    tmp_path: Path,
) -> None:
    script = cli.uninstall._windows_uninstall_script(
        Path(r"C:\uv\uv.exe"),
        tmp_path / "uninstall.log",
    )
    assert "AddSeconds(15)" in script
    assert "$retryInterval = 500" in script
    assert "Attempt {0}: uv tool uninstall agent-relay" in script
    assert "Final result: success after" in script
    assert "Final result: failure after" in script
    assert "Stop other Agent Relay" in script


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows file locking")
@pytest.mark.parametrize(
    ("lock_seconds", "timeout_seconds", "expected"),
    [(4.0, 8, "success"), (30, 3, "failure")],
)
def test_windows_uninstall_retries_real_locked_executable(
    tmp_path: Path,
    lock_seconds: float,
    timeout_seconds: int,
    expected: str,
) -> None:
    """A real executable lock is retried until release or the deadline."""
    if Path(sys.executable).suffix.lower() != ".exe":
        pytest.skip("a native Python executable is required")
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if powershell is None:
        pytest.skip("PowerShell is unavailable")

    tool_root = tmp_path / "uv" / "tools" / "agent-relay"
    tool_root.mkdir(parents=True)
    running_executable = tool_root / "Scripts" / "agent-relay.exe"
    running_executable.parent.mkdir()
    shutil.copy2(sys.executable, running_executable)
    ready = tmp_path / "ready"
    code = (
        "from pathlib import Path; import time; "
        f"Path({str(ready)!r}).write_text('ready'); time.sleep({lock_seconds})"
    )
    process = subprocess.Popen([str(running_executable), "-c", code])
    lock_ready = tmp_path / "lock-ready"
    lock_code = (
        "import ctypes, sys, time; "
        "from pathlib import Path; "
        "path, ready, seconds = sys.argv[1], Path(sys.argv[2]), float(sys.argv[3]); "
        "kernel32 = ctypes.WinDLL('kernel32', use_last_error=True); "
        "create = kernel32.CreateFileW; "
        "create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, "
        "ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]; "
        "create.restype = ctypes.c_void_p; "
        "handle = create(path, 0x80000000, 0, None, 3, 0x80, None); "
        "invalid = ctypes.c_void_p(-1).value; "
        "assert handle not in (None, invalid), ctypes.get_last_error(); "
        "ready.write_text('ready'); time.sleep(seconds); kernel32.CloseHandle(handle)"
    )
    lock_process = subprocess.Popen(
        [sys.executable, "-c", lock_code, str(running_executable), str(lock_ready), str(lock_seconds)]
    )
    log_path = tmp_path / "uninstall.log"
    fake_uv = tmp_path / "uv.cmd"
    target_literal = str(running_executable).replace("'", "''")
    fake_uv.write_text(
        "@echo off\r\n"
        f"powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "
        f"\"$ErrorActionPreference='Stop'; try {{ Remove-Item -LiteralPath '{target_literal}' -Force -ErrorAction Stop; exit 0 }} catch {{ exit 5 }}\"\r\n"
        "exit /b %ERRORLEVEL%\r\n",
        encoding="utf-8",
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "the copied executable did not start"
        deadline = time.monotonic() + 10
        while not lock_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert lock_ready.exists(), "the executable lock helper did not start"
        assert lock_process.poll() is None, "the temporary executable lock was released early"
        script = cli.uninstall._windows_uninstall_script(
            fake_uv,
            log_path,
            timeout_seconds=timeout_seconds,
            retry_interval_milliseconds=100,
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        completed = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_seconds + 10,
        )
        assert completed.returncode == (0 if expected == "success" else 1)
        log = log_path.read_text(encoding="utf-8")
        assert f"Final result: {expected}" in log
        assert "Attempt 1" in log
        if expected == "success":
            assert "Attempt 2" in log
        if expected == "failure":
            assert "Stop other Agent Relay" in log
            assert running_executable.exists()
        else:
            assert not running_executable.exists()
    finally:
        for child in (lock_process, process):
            child.terminate()
            child.wait(timeout=10)
        shutil.rmtree(tool_root, ignore_errors=True)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows file locking")
def test_windows_uninstall_real_process_lock_boundary(tmp_path: Path) -> None:
    """A running executable in the uv tool tree must block its deletion."""
    if Path(sys.executable).suffix.lower() != ".exe":
        pytest.skip("a native Python executable is required")
    tool_root = tmp_path / "uv" / "tools" / "agent-relay"
    tool_root.mkdir(parents=True)
    running_executable = tool_root / "Scripts" / "agent-relay.exe"
    running_executable.parent.mkdir()
    shutil.copy2(sys.executable, running_executable)
    ready = tmp_path / "ready"
    code = (
        "from pathlib import Path; import time; "
        f"Path({str(ready)!r}).write_text('ready'); time.sleep(30)"
    )
    process = subprocess.Popen([str(running_executable), "-c", code])
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "the copied executable did not start"
        with pytest.raises(OSError):
            running_executable.unlink()
    finally:
        process.terminate()
        process.wait(timeout=10)
    shutil.rmtree(tool_root)

"""User-scoped Agent Relay uninstallation helpers."""

from __future__ import annotations

import base64
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from . import config

WINDOWS_UNINSTALL_TIMEOUT_SECONDS = 15
WINDOWS_UNINSTALL_RETRY_INTERVAL_MILLISECONDS = 500


def find_uv() -> Path | None:
    """Find uv in PATH or in the supported installer locations."""
    discovered = shutil.which("uv")
    if discovered:
        return Path(discovered)
    home = Path.home()
    if os.name == "nt":
        candidates = [home / ".local" / "bin" / "uv.exe"]
        if local_app_data := os.environ.get("LOCALAPPDATA"):
            candidates.extend(
                (
                    Path(local_app_data) / "uv" / "uv.exe",
                    Path(local_app_data) / "Programs" / "uv" / "uv.exe",
                )
            )
    else:
        candidates = [home / ".local" / "bin" / "uv", home / ".cargo" / "bin" / "uv"]
    for candidate in candidates:
        if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
            return candidate
    return None


def _find_powershell() -> Path | None:
    for name in ("powershell.exe", "pwsh.exe"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _windows_uninstall_script(
    uv_path: Path,
    log_path: Path,
    *,
    timeout_seconds: int = WINDOWS_UNINSTALL_TIMEOUT_SECONDS,
    retry_interval_milliseconds: int = WINDOWS_UNINSTALL_RETRY_INTERVAL_MILLISECONDS,
) -> str:
    """Build the bounded retry script executed after the CLI exits."""
    uv_literal = _powershell_literal(str(uv_path))
    log_literal = _powershell_literal(str(log_path))
    return (
        "$ErrorActionPreference = 'Continue'; "
        f"$deadline = [DateTime]::UtcNow.AddSeconds({timeout_seconds}); "
        "$attempt = 0; "
        "while ([DateTime]::UtcNow -lt $deadline) { "
        "$attempt += 1; "
        f"Add-Content -LiteralPath {log_literal} "
        "-Value ('Attempt {0}: uv tool uninstall agent-relay' -f $attempt); "
        f"& {uv_literal} tool uninstall agent-relay >> {log_literal} 2>&1; "
        "$exitCode = [int]$LASTEXITCODE; "
        f"Add-Content -LiteralPath {log_literal} "
        "-Value ('Attempt {0} result: exit code {1}' -f $attempt, $exitCode); "
        "if ($exitCode -eq 0) { "
        f"Add-Content -LiteralPath {log_literal} -Value 'Final result: success'; "
        "exit 0 }; "
        f"$remainingMilliseconds = ($deadline - [DateTime]::UtcNow).TotalMilliseconds; "
        "if ($remainingMilliseconds -le 0) { break }; "
        f"Start-Sleep -Milliseconds ([int][Math]::Min({retry_interval_milliseconds}, "
        "[Math]::Ceiling($remainingMilliseconds))); "
        "} "
        f"Add-Content -LiteralPath {log_literal} -Value 'Final result: failure'; "
        f"Add-Content -LiteralPath {log_literal} -Value "
        "'Stop other Agent Relay server or agent processes, then run: uv tool uninstall agent-relay'; "
        "exit 1"
    )


def _schedule_windows_uninstall(uv_path: Path) -> Path:
    """Delegate uv removal until this process has released its own exe lock."""
    powershell = _find_powershell()
    if powershell is None:
        raise config.ConfigError(
            "PowerShell was not found; stop Agent Relay processes and run "
            "uv tool uninstall agent-relay from a separate PowerShell window"
        )
    try:
        descriptor, log_name = tempfile.mkstemp(
            prefix="agent-relay-uninstall-", suffix=".log"
        )
        os.close(descriptor)
    except OSError as exc:
        raise config.ConfigError("could not prepare the Windows uninstall handoff") from exc
    log_path = Path(log_name)
    script = _windows_uninstall_script(uv_path, log_path)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        subprocess.Popen(
            [
                str(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            ),
        )
    except OSError as exc:
        log_path.unlink(missing_ok=True)
        raise config.ConfigError("could not schedule Windows uninstallation") from exc
    return log_path


def uninstall_tool() -> Path | None:
    """Remove or safely delegate removal of the user-scoped uv tool install."""
    uv_path = find_uv()
    if uv_path is None:
        raise config.ConfigError(
            "uv was not found; uninstall the uv-managed Agent Relay installation "
            "with uv available on PATH"
        )
    if os.name == "nt":
        return _schedule_windows_uninstall(uv_path)
    try:
        result = subprocess.run(
            [str(uv_path), "tool", "uninstall", "agent-relay"],
            check=False,
        )
    except OSError as exc:
        raise config.ConfigError("could not start uv for uninstallation") from exc
    if result.returncode != 0:
        raise config.ConfigError(
            f"uv tool uninstall agent-relay failed with exit code {result.returncode}"
        )
    return None


def validate_purge_target(data_dir: Path) -> None:
    """Validate the exact default data directory before a recursive removal."""
    expected_name = config.CONFIG_DIR_NAME
    if data_dir.name != expected_name:
        raise config.ConfigError(
            f"refusing to purge an unexpected Agent Relay data directory: {data_dir}"
        )
    try:
        info = data_dir.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise config.ConfigError("Agent Relay data directory could not be inspected") from exc
    is_junction = getattr(data_dir, "is_junction", lambda: False)()
    if stat.S_ISLNK(info.st_mode) or data_dir.is_symlink() or is_junction:
        raise config.ConfigError(
            "refusing to purge a symbolic-link or junction Agent Relay data directory"
        )
    if not stat.S_ISDIR(info.st_mode):
        raise config.ConfigError("Agent Relay data path is not a directory")


def purge_data(data_dir: Path) -> bool:
    """Remove the validated default data directory and report whether it existed."""
    validate_purge_target(data_dir)
    if not data_dir.exists():
        return False
    try:
        shutil.rmtree(data_dir)
    except OSError as exc:
        raise config.ConfigError("Agent Relay data directory could not be removed") from exc
    return True

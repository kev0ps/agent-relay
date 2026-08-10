"""User-scoped Agent Relay uninstallation helpers."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

from . import config


def _candidate_uv_paths() -> tuple[Path, ...]:
    """Return the per-user uv paths used by the supported installers."""
    candidates: list[Path] = []
    if os.name == "nt":
        candidates.append(Path.home() / ".local" / "bin" / "uv.exe")
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.extend(
                (
                    Path(local_app_data) / "uv" / "uv.exe",
                    Path(local_app_data) / "Programs" / "uv" / "uv.exe",
                )
            )
    else:
        candidates.extend(
            (
                Path.home() / ".local" / "bin" / "uv",
                Path.home() / ".cargo" / "bin" / "uv",
            )
        )
    return tuple(candidates)


def find_uv() -> Path | None:
    """Find uv without relying on a shell or a mutable working directory."""
    discovered = shutil.which("uv")
    if discovered:
        return Path(discovered)
    for candidate in _candidate_uv_paths():
        if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
            return candidate
    return None


def uninstall_tool() -> None:
    """Remove the user-scoped uv tool installation of Agent Relay."""
    uv_path = find_uv()
    if uv_path is None:
        raise config.ConfigError(
            "uv was not found; uninstall the uv-managed Agent Relay installation "
            "with uv available on PATH"
        )
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

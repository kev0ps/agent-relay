"""Minimal discovery and command construction for preinstalled Google Chrome."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .common import E2EError


def find_chrome() -> Path:
    """Return preinstalled Google Chrome or fail without provisioning it."""
    candidates: list[Path] = []
    for name in ("google-chrome", "google-chrome-stable", "chrome.exe", "chrome"):
        if resolved := shutil.which(name):
            candidates.append(Path(resolved))
    if sys.platform == "win32":
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            if root := os.environ.get(variable):
                candidates.append(
                    Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
                )
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    raise E2EError("Google Chrome is not installed on this E2E runner")


def chrome_command(
    *,
    executable: Path,
    profile: Path,
    fixture_url: str,
) -> list[str]:
    """Build the single headed Chrome command used by both CUA platforms."""
    if not executable.is_absolute() or not profile.is_absolute():
        raise ValueError("Chrome executable and profile must be absolute paths")
    parsed = urlsplit(fixture_url)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Chrome fixture must use loopback HTTP")
    return [
        str(executable),
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-extensions",
        "--disable-default-apps",
        "--new-window",
        "--window-position=0,0",
        "--window-size=1280,720",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--force-renderer-accessibility",
        fixture_url,
    ]

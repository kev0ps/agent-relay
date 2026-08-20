from __future__ import annotations

from pathlib import Path

import pytest

from scripts.e2e import chrome
from scripts.e2e.common import E2EError


def test_find_chrome_uses_preinstalled_linux_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "google-chrome"
    executable.write_bytes(b"binary")
    monkeypatch.setattr(chrome.sys, "platform", "linux")
    monkeypatch.setattr(
        chrome.shutil,
        "which",
        lambda name: str(executable) if name == "google-chrome" else None,
    )

    assert chrome.find_chrome() == executable


def test_find_chrome_resolves_a_system_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "google-chrome-real"
    target.write_bytes(b"binary")
    link = tmp_path / "google-chrome"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this host")
    monkeypatch.setattr(chrome.sys, "platform", "linux")
    monkeypatch.setattr(
        chrome.shutil,
        "which",
        lambda name: str(link) if name == "google-chrome" else None,
    )

    assert chrome.find_chrome() == target


def test_find_chrome_uses_preinstalled_windows_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "Google" / "Chrome" / "Application" / "chrome.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"binary")
    monkeypatch.setattr(chrome.sys, "platform", "win32")
    monkeypatch.setattr(chrome.shutil, "which", lambda _name: None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    assert chrome.find_chrome() == executable


def test_find_chrome_fails_explicitly_without_provisioning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chrome.sys, "platform", "linux")
    monkeypatch.setattr(chrome.shutil, "which", lambda _name: None)

    with pytest.raises(E2EError, match="Google Chrome is not installed"):
        chrome.find_chrome()


def test_chrome_command_is_headed_and_uses_an_isolated_profile(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "chrome"
    profile = tmp_path / "profile"

    command = chrome.chrome_command(
        executable=executable,
        profile=profile,
        fixture_url="http://127.0.0.1:8898/",
    )

    assert command[0] == str(executable)
    assert f"--user-data-dir={profile}" in command
    assert "--new-window" in command
    assert "--force-renderer-accessibility" in command
    assert "--headless" not in command
    assert command[-1] == "http://127.0.0.1:8898/"


def test_chrome_command_rejects_credentials_and_non_loopback_hosts(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="loopback"):
        chrome.chrome_command(
            executable=tmp_path / "chrome",
            profile=tmp_path / "profile",
            fixture_url="http://127.0.0.1:8898@attacker.example/",
        )

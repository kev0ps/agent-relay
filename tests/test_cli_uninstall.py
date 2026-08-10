from __future__ import annotations

import os
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
    monkeypatch.setattr(cli.uninstall, "find_uv", lambda: Path("uv"))

    def run(_command: list[str], *, check: bool) -> SimpleNamespace:
        assert check is False
        raise PermissionError("not exposed to the user")

    monkeypatch.setattr(cli.uninstall.subprocess, "run", run)

    assert cli.main(["uninstall"]) == 1
    captured = capsys.readouterr()
    assert "could not start uv" in captured.err
    assert "not exposed" not in captured.err

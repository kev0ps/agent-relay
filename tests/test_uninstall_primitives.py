from __future__ import annotations

from pathlib import Path

from agent_relay import uninstall


def test_uninstall_keeps_only_the_required_safe_system_primitives() -> None:
    source = Path(uninstall.__file__).read_text(encoding="utf-8")

    assert "def _is_windows" not in source
    assert "def _run_uv_uninstall" not in source
    assert '"-WindowStyle"' not in source
    assert 'shutil.which("uv")' in source
    assert "def find_uv" in source
    assert "def validate_purge_target" in source
    assert "def purge_data" in source
    assert "data_dir.lstat()" in source
    assert "data_dir.is_symlink()" in source
    assert "is_junction" in source
    assert "shutil.rmtree(data_dir)" in source


def test_find_uv_uses_the_supported_installer_fallback(monkeypatch, tmp_path: Path) -> None:
    fallback = tmp_path / ".local" / "bin" / "uv"
    fallback.parent.mkdir(parents=True)
    fallback.touch(mode=0o700)
    monkeypatch.setattr(uninstall.shutil, "which", lambda _name: None)
    monkeypatch.setattr(uninstall.Path, "home", classmethod(lambda _cls: tmp_path))

    assert uninstall.find_uv() == fallback


def test_windows_handoff_stays_bounded_without_extra_process_window_flags() -> None:
    script = uninstall._windows_uninstall_script(
        Path(r"C:\\uv\\uv.exe"),
        Path(r"C:\\Temp\\agent-relay-uninstall.log"),
    )

    assert "AddSeconds(15)" in script
    assert "$remainingMilliseconds" in script
    assert "Start-Sleep -Milliseconds ([int][Math]::Min(500" in script
    assert "$attempt = 0" in script
    assert "Attempt {0}: uv tool uninstall agent-relay" in script
    assert "Attempt {0} result: exit code {1}" in script
    assert "Final result: success" in script
    assert "Final result: failure" in script

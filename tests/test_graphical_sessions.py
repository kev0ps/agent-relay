from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.e2e.common import E2EError
from scripts.e2e.platform import linux_graphics, windows_graphics


class FakePlatform:
    name = "Fake"
    device_id = "fake"
    run_prefix = "fake"
    cua_run_prefix = "fake-cua"

    def minimal_environment(
        self,
        home: Path,
        values: dict[str, str],
    ) -> dict[str, str]:
        return {"HOME": str(home), **values}

    def spawn(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("spawn should not be reached")


def test_linux_session_rejects_non_x86_64_before_starting_processes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(linux_graphics.sys, "platform", "linux")
    monkeypatch.setattr(linux_graphics.platform_module, "machine", lambda: "aarch64")

    with pytest.raises(E2EError, match="requires x86_64"):
        linux_graphics.LinuxGraphicalSession().prepare(
            FakePlatform(),
            root=tmp_path,
            home=tmp_path / "home",
            repository=tmp_path,
        )


def test_linux_at_spi_address_parser_accepts_only_the_bounded_gdbus_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        linux_graphics.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="('unix:path=/tmp/at-spi-bus',)\n",
        ),
    )

    assert linux_graphics._read_at_spi_bus_address({}) == (
        "unix:path=/tmp/at-spi-bus"
    )


def test_windows_session_rejects_session_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(windows_graphics, "current_session_id", lambda: 0)

    with pytest.raises(E2EError, match="Session 0"):
        windows_graphics.WindowsGraphicalSession().prepare(
            FakePlatform(),
            root=tmp_path,
            home=tmp_path / "home",
            repository=tmp_path,
        )


def test_windows_session_returns_only_the_graphical_driver_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(windows_graphics, "current_session_id", lambda: 7)

    environment = windows_graphics.WindowsGraphicalSession().prepare(
        FakePlatform(),
        root=tmp_path,
        home=tmp_path / "home",
        repository=tmp_path,
    )

    assert environment["CUA_DRIVER_TELEMETRY"] == "0"
    assert environment["CUA_DRIVER_RS_TELEMETRY_ENABLED"] == "0"
    assert environment["HOME"] == str(tmp_path / "home")

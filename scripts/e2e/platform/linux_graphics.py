"""Linux X11, D-Bus, Openbox, and AT-SPI session primitives."""

from __future__ import annotations

import fcntl
import os
import platform as platform_module
import re
import select
import subprocess
import sys
from pathlib import Path

from ..common import E2EError
from ..terminal import ProcessPlatform, _wait_for
from .posix import PosixProcessManager, terminate_process_group

DISPLAY_MIN = 91
DISPLAY_MAX = 120
DESKTOP_READY_TIMEOUT_SECONDS = 15.0


def _acquire_display() -> tuple[str, int]:
    for number in range(DISPLAY_MIN, DISPLAY_MAX + 1):
        lock_path = Path("/tmp") / f"agent-relay-e2e-display-{number}.lock"
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            continue
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            os.close(lock_fd)
            continue
        if (Path("/tmp/.X11-unix") / f"X{number}").exists():
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
            continue
        return f":{number}", lock_fd
    raise E2EError("no isolated X11 display is available")


def _release_display(lock_fd: int | None) -> None:
    if lock_fd is None:
        return
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def _run_ready(
    command: list[str],
    environment: dict[str, str],
    repository: Path,
) -> bool:
    try:
        subprocess.run(
            command,
            env=environment,
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=2,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _x11_ready(environment: dict[str, str], repository: Path) -> bool:
    return _run_ready(
        ["xdpyinfo", "-display", environment["DISPLAY"]],
        environment,
        repository,
    )


def _accessibility_ready(
    environment: dict[str, str],
    repository: Path,
) -> bool:
    return _run_ready(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.a11y.Bus",
            "--object-path",
            "/org/a11y/bus",
            "--method",
            "org.a11y.Bus.GetAddress",
        ],
        environment,
        repository,
    )


def _enable_accessibility(
    environment: dict[str, str],
    repository: Path,
) -> bool:
    try:
        for property_name in ("ScreenReaderEnabled", "IsEnabled"):
            subprocess.run(
                [
                    "gdbus",
                    "call",
                    "--session",
                    "--dest",
                    "org.a11y.Bus",
                    "--object-path",
                    "/org/a11y/bus",
                    "--method",
                    "org.freedesktop.DBus.Properties.Set",
                    "org.a11y.Status",
                    property_name,
                    "<true>",
                ],
                env=environment,
                cwd=repository,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=2,
                shell=False,
            )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _read_at_spi_bus_address(
    environment: dict[str, str],
    repository: Path | None = None,
) -> str:
    try:
        completed = subprocess.run(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.a11y.Bus",
                "--object-path",
                "/org/a11y/bus",
                "--method",
                "org.a11y.Bus.GetAddress",
            ],
            env=environment,
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
            shell=False,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise E2EError("AT-SPI bus address is unavailable") from error
    match = re.fullmatch(r"\('(?P<address>unix:[^']+)'\s*,?\)\s*", completed.stdout)
    if completed.returncode != 0 or match is None:
        raise E2EError("AT-SPI bus address is invalid")
    return match.group("address")


def _start_dbus(
    environment: dict[str, str],
    manager: PosixProcessManager,
    repository: Path,
) -> str:
    process = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address=1"],
        cwd=repository,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        shell=False,
        text=True,
    )
    manager.add_cleanup(lambda: terminate_process_group(process))
    if process.stdout is None:
        raise E2EError("D-Bus stdout is unavailable")
    ready, _, _ = select.select(
        [process.stdout],
        [],
        [],
        DESKTOP_READY_TIMEOUT_SECONDS,
    )
    if not ready:
        raise E2EError("D-Bus startup timed out")
    address = process.stdout.readline().strip()
    if not address.startswith("unix:"):
        raise E2EError("D-Bus address is invalid")
    return address


class LinuxGraphicalSession:
    """Prepare an isolated headed Linux desktop and nothing else."""

    def __init__(self) -> None:
        self._display_lock_fd: int | None = None

    def _release_display(self) -> None:
        lock_fd = self._display_lock_fd
        self._display_lock_fd = None
        _release_display(lock_fd)

    def prepare(
        self,
        platform: ProcessPlatform,
        *,
        root: Path,
        home: Path,
        repository: Path,
    ) -> dict[str, str]:
        if sys.platform != "linux":
            raise E2EError("Linux graphical session requires Linux")
        if platform_module.machine() != "x86_64":
            raise E2EError("Linux graphical session requires x86_64")
        if not isinstance(platform, PosixProcessManager):
            raise E2EError("Linux graphical session requires POSIX process primitives")
        runtime_dir = root / "runtime"
        runtime_dir.mkdir(mode=0o700)
        display, self._display_lock_fd = _acquire_display()
        platform.add_cleanup(self._release_display)
        environment = platform.minimal_environment(
            home,
            {
                "DISPLAY": display,
                "ACCESSIBILITY_ENABLED": "1",
                "NO_AT_BRIDGE": "0",
                "GTK_MODULES": "gail:atk-bridge",
                "QT_ACCESSIBILITY": "1",
                "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
                "CUA_DRIVER_TELEMETRY": "0",
                "CUA_DRIVER_RS_TELEMETRY_ENABLED": "0",
                "CUA_E2E_BROWSER_NO_SANDBOX": "1",
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_RUNTIME_DIR": str(runtime_dir),
            },
        )
        xvfb = platform.spawn(
            ["Xvfb", display, "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
            environment=environment,
            cwd=repository,
            label="xvfb",
        )
        _wait_for(
            "Linux CUA X11",
            lambda: _x11_ready(environment, repository),
            timeout=DESKTOP_READY_TIMEOUT_SECONDS,
        )
        if xvfb.poll() is not None:
            raise E2EError("Xvfb exited during startup")
        environment["DBUS_SESSION_BUS_ADDRESS"] = _start_dbus(
            environment,
            platform,
            repository,
        )
        _wait_for(
            "Linux CUA accessibility bus",
            lambda: _accessibility_ready(environment, repository),
            timeout=DESKTOP_READY_TIMEOUT_SECONDS,
        )
        _wait_for(
            "Linux CUA accessibility status",
            lambda: _enable_accessibility(environment, repository),
            timeout=DESKTOP_READY_TIMEOUT_SECONDS,
        )
        environment["AT_SPI_BUS_ADDRESS"] = _read_at_spi_bus_address(
            environment,
            repository,
        )
        openbox = platform.spawn(
            ["openbox"],
            environment=environment,
            cwd=repository,
            label="openbox",
        )
        if openbox.poll() is not None:
            raise E2EError("Openbox exited during startup")
        return environment

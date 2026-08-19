#!/usr/bin/env python3
"""Run the bounded Linux Computer Use Agent Relay scenario."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Sequence


def _load_support() -> Any:
    name = "_agent_relay_linux_cua_adapter"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    path = Path(__file__).with_name("linux_cua_adapter.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load Linux CUA adapter")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


support = _load_support()
harness = support.harness
native = support.native
re = support.re
shutil = support.shutil
subprocess = support.subprocess
portable_mcp = support.portable_mcp
portable_oracles = support.portable_oracles
portable_scenarios = support.portable_scenarios

ROOT = support.ROOT
DESKTOP_FIXTURE = support.DESKTOP_FIXTURE
DISPLAY = support.DISPLAY
COMPUTER_APP_NAME = support.COMPUTER_APP_NAME
COMPUTER_WINDOW_TITLE = support.COMPUTER_WINDOW_TITLE
SNAP_CHROMIUM_BIN_DIR = support.SNAP_CHROMIUM_BIN_DIR
DESKTOP_READY_TIMEOUT_SECONDS = support.DESKTOP_READY_TIMEOUT_SECONDS
FIXTURE_READY_TIMEOUT_SECONDS = support.FIXTURE_READY_TIMEOUT_SECONDS
AGENT_READY_TIMEOUT_SECONDS = support.AGENT_READY_TIMEOUT_SECONDS
CUA_EXISTING_PROFILE_GRANT_ENV = support.CUA_EXISTING_PROFILE_GRANT_ENV
CUA_CAPABILITIES = support.CUA_CAPABILITIES
CUA_AGENT_TOOLS = support.CUA_AGENT_TOOLS
LinuxCuaE2EError = support.LinuxCuaE2EError


def _runtime(*args: Any, **kwargs: Any) -> Any:
    return support._runtime(*args, **kwargs)


def _cua_agent_driver_environment() -> dict[str, str]:
    return support._cua_agent_driver_environment()


def _cua_snapshot_diagnostic(result: Any) -> str:
    return support._cua_snapshot_diagnostic(result)


def _cua_controls_ready(runtime: Any) -> bool:
    return support._cua_controls_ready(runtime)


def _launch_cua_browser(runtime: Any, profile: Path, executable: Path) -> int:
    return support._launch_cua_browser(runtime, profile, executable)


def _kill_cua_browser(runtime: Any, pid: int) -> None:
    return support._kill_cua_browser(runtime, pid)


def _status(*args: Any, **kwargs: Any) -> None:
    return support._status(*args, **kwargs)


def _fixture_ready(url: str) -> bool:
    return support._fixture_ready(url)


def _start_dbus(environment: dict[str, str], lifecycle: Any) -> str:
    return support._start_dbus(environment, lifecycle)


def _run_ready(command: list[str], environment: dict[str, str]) -> bool:
    return support._run_ready(command, environment)


def _x11_ready(environment: dict[str, str]) -> bool:
    return support._x11_ready(environment)


def _accessibility_ready(environment: dict[str, str]) -> bool:
    return support._accessibility_ready(environment)


def _enable_chromium_accessibility(environment: dict[str, str]) -> bool:
    return support._enable_chromium_accessibility(environment)


def _read_at_spi_bus_address(environment: dict[str, str]) -> str:
    return support._read_at_spi_bus_address(environment)


def _x11_window_hint(environment: dict[str, str]) -> str:
    return support._x11_window_hint(environment)


def _x11_has_client_window(environment: dict[str, str]) -> bool:
    return support._x11_has_client_window(environment)


def _x11_search_ids(
    environment: dict[str, str], option: str, value: str
) -> set[str]:
    return support._x11_search_ids(environment, option, value)


def _x11_has_expected_window(environment: dict[str, str]) -> bool:
    return support._x11_has_expected_window(environment)


def _resolve_chromium() -> Path:
    return support._resolve_chromium()


def chromium_command(executable: Path, profile: Path, fixture_url: str) -> list[str]:
    return support.chromium_command(executable, profile, fixture_url)


def chromium_environment(
    executable: Path,
    environment: dict[str, str],
    *,
    host_runtime_dir: Path | None,
    host_session_bus_address: str | None,
) -> dict[str, str]:
    original = support.SNAP_CHROMIUM_BIN_DIR
    support.SNAP_CHROMIUM_BIN_DIR = SNAP_CHROMIUM_BIN_DIR
    try:
        return support.chromium_environment(
            executable,
            environment,
            host_runtime_dir=host_runtime_dir,
            host_session_bus_address=host_session_bus_address,
        )
    finally:
        support.SNAP_CHROMIUM_BIN_DIR = original


def _is_snap_chromium_launcher(executable: Path) -> bool:
    original = support.SNAP_CHROMIUM_BIN_DIR
    support.SNAP_CHROMIUM_BIN_DIR = SNAP_CHROMIUM_BIN_DIR
    try:
        return support._is_snap_chromium_launcher(executable)
    finally:
        support.SNAP_CHROMIUM_BIN_DIR = original


def _stderr_hint(path: Path) -> str | None:
    return support._stderr_hint(path)


def _event_hint(path: Path) -> str:
    return support._event_hint(path)


def run_scenario(
    evidence_dir: Path | None = None, *, output_file: Path | None = None
) -> None:
    """Run real Server + Agent + public MCP Computer Use calls under Xvfb."""
    harness.run_cua_scenario(
        support.make_adapter(), evidence_dir, output_file=output_file
    )


def main(argv: Sequence[str] | None = None) -> int:
    return harness.run_entrypoint(
        "Linux Computer Use Agent Relay smoke", run_scenario, argv
    )


if __name__ == "__main__":
    raise SystemExit(main())

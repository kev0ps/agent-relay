#!/usr/bin/env python3
"""Run the experimental Windows CUA Agent Relay scenario."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Sequence


def _load_support() -> Any:
    name = "_agent_relay_windows_cua_adapter"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    path = Path(__file__).with_name("windows_cua_adapter.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load Windows CUA adapter")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


support = _load_support()
windows = support.windows
harness = support.harness
portable_mcp = support.portable_mcp
portable_scenarios = support.portable_scenarios

ROOT = support.ROOT
FIXTURE = support.FIXTURE
DEVICE_ID = support.DEVICE_ID
COMPUTER_APP_NAME = support.COMPUTER_APP_NAME
COMPUTER_WINDOW_TITLE = support.COMPUTER_WINDOW_TITLE
CUA_CAPABILITIES = support.CUA_CAPABILITIES
FIXTURE_READY_TIMEOUT_SECONDS = support.FIXTURE_READY_TIMEOUT_SECONDS
AGENT_READY_TIMEOUT_SECONDS = support.AGENT_READY_TIMEOUT_SECONDS
MAX_DIAGNOSTIC_BYTES = support.MAX_DIAGNOSTIC_BYTES
WindowsCuaE2EError = support.WindowsCuaE2EError


def _load_windows_harness() -> Any:
    return windows


def _current_session_id() -> int:
    return support._current_session_id()


def _powershell_executable() -> Path:
    return support._powershell_executable()


def _runtime(*, mcp_url: str, control_token: str, run_id: str, fixtures_root: Path) -> Any:
    return support._runtime(
        mcp_url=mcp_url,
        control_token=control_token,
        run_id=run_id,
        fixtures_root=fixtures_root,
    )


def _status(
    mcp_url: str,
    control_token: str,
    *,
    connected: bool,
    allow_unenrolled: bool = False,
) -> None:
    return support._status(
        mcp_url,
        control_token,
        connected=connected,
        allow_unenrolled=allow_unenrolled,
    )


def _fixture_ready(path: Path) -> bool:
    return support._fixture_ready(path)


def _driver_environment(values: dict[str, str]) -> dict[str, str]:
    return support._driver_environment(values)


def _fixture_command(event_path: Path, ready_path: Path, run_id: str) -> list[str]:
    return support._fixture_command(event_path, ready_path, run_id)


def run_scenario(
    evidence_dir: Path | None = None, *, output_file: Path | None = None
) -> None:
    """Run Server + Agent + WinForms fixture through public MCP CUA tools."""
    harness.run_cua_scenario(
        support.make_adapter(), evidence_dir, output_file=output_file
    )


def main(argv: Sequence[str] | None = None) -> int:
    return harness.run_entrypoint(
        "Windows Computer Use Agent Relay E2E", run_scenario, argv
    )


if __name__ == "__main__":
    raise SystemExit(main())

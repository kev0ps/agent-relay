#!/usr/bin/env python3
"""Run the minimal Linux Agent Relay MCP scenario."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def _load_support() -> Any:
    name = "_agent_relay_linux_e2e_adapter"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    path = Path(__file__).with_name("linux_e2e_adapter.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load Linux E2E adapter")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


support = _load_support()
harness = support.harness
signal = support.signal
portable_mcp = support.portable_mcp
portable_oracles = support.portable_oracles
portable_scenarios = support.portable_scenarios

DEVICE_ID = support.DEVICE_ID
CORE_CAPABILITIES = support.CORE_CAPABILITIES
POLL_INTERVAL_SECONDS = support.POLL_INTERVAL_SECONDS
SERVER_READY_TIMEOUT_SECONDS = harness.SERVER_READY_TIMEOUT_SECONDS
AGENT_READY_TIMEOUT_SECONDS = harness.AGENT_READY_TIMEOUT_SECONDS
PROCESS_STOP_TIMEOUT_SECONDS = support.PROCESS_STOP_TIMEOUT_SECONDS
MAX_TOKEN_LENGTH = support.MAX_TOKEN_LENGTH
NativeE2EError = support.NativeE2EError
NativeLifecycle = support.NativeLifecycle


def generate_credentials() -> tuple[str, str]:
    return support.generate_credentials()


def choose_loopback_port() -> int:
    return support.choose_loopback_port()


def server_command(port: int) -> list[str]:
    return support.server_command(port)


def agent_command(port: int, workspace: Path) -> list[str]:
    return support.agent_command(port, workspace)


def wait_for_process_exit(
    process: subprocess.Popen[Any] | None, *, timeout: float
) -> bool:
    return support.wait_for_process_exit(process, timeout=timeout)


def _process_group_has_live_members(group_id: int) -> bool:
    return support._process_group_has_live_members(group_id)


def terminate_process_group(
    process: subprocess.Popen[Any],
    *,
    timeout: float = PROCESS_STOP_TIMEOUT_SECONDS,
    process_group_id: int | None = None,
) -> None:
    return support.terminate_process_group(
        process,
        timeout=timeout,
        process_group_id=process_group_id,
    )


def _minimal_environment(home: Path, values: dict[str, str]) -> dict[str, str]:
    return support._minimal_environment(home, values)


def _spawn(
    argv: Sequence[str],
    *,
    environment: dict[str, str],
    cwd: Path,
    lifecycle: NativeLifecycle,
    stderr_path: Path | None = None,
) -> subprocess.Popen[Any]:
    return support._spawn(
        argv,
        environment=environment,
        cwd=cwd,
        lifecycle=lifecycle,
        stderr_path=stderr_path,
    )


def _status(
    mcp_url: str,
    control_token: str,
    *,
    connected: bool,
    expected_capabilities: tuple[str, ...] | None = None,
    allow_unenrolled: bool = False,
) -> None:
    return support._status(
        mcp_url,
        control_token,
        connected=connected,
        expected_capabilities=expected_capabilities,
        allow_unenrolled=allow_unenrolled,
    )


def _server_endpoint_available(mcp_url: str, control_token: str) -> bool:
    return support._server_endpoint_available(mcp_url, control_token)


def _runtime(
    *, mcp_url: str, control_token: str, run_id: str, fixtures_root: Path
) -> Any:
    return support._runtime(
        mcp_url=mcp_url,
        control_token=control_token,
        run_id=run_id,
        fixtures_root=fixtures_root,
    )


def _create_workspace(path: Path) -> None:
    return support._create_workspace(path)


def _write_artifact(evidence_dir: Path, name: str, payload: bytes) -> None:
    return support._write_artifact(evidence_dir, name, payload)


def _write_success(evidence_dir: Path) -> None:
    return support._write_success(evidence_dir)


def _terminal_adapter() -> Any:
    return harness.TerminalAdapter(
        label="Linux",
        device_id=DEVICE_ID,
        run_id_prefix="native-",
        temp_prefix="agent-relay-native-e2e-",
        success_message="Linux MCP end-to-end scenario passed.",
        failure_prefix="Linux E2E failed at scenario-",
        cleanup_message="Linux E2E cleanup failed",
        error_type=NativeE2EError,
        lifecycle_factory=lambda: NativeLifecycle(),
        minimal_environment=_minimal_environment,
        create_workspace=_create_workspace,
        spawn=lambda _role, argv, environment, cwd, context: _spawn(
            argv,
            environment=environment,
            cwd=cwd,
            lifecycle=context.lifecycle,
        ),
        stop_process=lambda process, _context: terminate_process_group(process),
        status=_status,
        endpoint_available=_server_endpoint_available,
        assert_owned=lambda _context, _phase: None,
        write_artifact=_write_artifact,
        report=lambda _context, _scenario_error, _cleanup_error: None,
    )


def run_scenario(
    evidence_dir: Path | None = None, *, output_file: Path | None = None
) -> None:
    """Run core MCP, offline detection, and real reconnect on Linux."""
    if os.name != "posix":
        raise NativeE2EError("Linux harness requires POSIX")
    harness.run_terminal_scenario(
        _terminal_adapter(), evidence_dir, output_file=output_file
    )


def main(argv: Sequence[str] | None = None) -> int:
    return harness.run_entrypoint("Linux Agent Relay E2E", run_scenario, argv)


if __name__ == "__main__":
    raise SystemExit(main())

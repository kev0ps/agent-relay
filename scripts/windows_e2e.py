#!/usr/bin/env python3
"""Run the minimal Windows Agent Relay MCP scenario."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def _load_support() -> Any:
    name = "_agent_relay_windows_e2e_adapter"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    path = Path(__file__).with_name("windows_e2e_adapter.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load Windows E2E adapter")
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
MAX_ARTIFACT_BYTES = support.MAX_ARTIFACT_BYTES
MAX_DIAGNOSTIC_BYTES = support.MAX_DIAGNOSTIC_BYTES
DIAGNOSTIC_CHUNK_BYTES = support.DIAGNOSTIC_CHUNK_BYTES
WindowsE2EError = support.WindowsE2EError
WindowsJob = support.WindowsJob
WindowsLifecycle = support.WindowsLifecycle
_BasicAccountingInformation = support._BasicAccountingInformation
_LargeInteger = support._LargeInteger


def generate_credentials() -> tuple[str, str]:
    return support.generate_credentials()


def choose_loopback_port() -> int:
    return support.choose_loopback_port()


def _windows_system_directory() -> Path:
    return support._windows_system_directory()


def server_command(port: int) -> list[str]:
    return support.server_command(port)


def agent_command(port: int, workspace: Path) -> list[str]:
    return support.agent_command(port, workspace)


def minimal_environment(home: Path, values: dict[str, str]) -> dict[str, str]:
    return support.minimal_environment(home, values)


def _spawn(
    argv: Sequence[str],
    *,
    environment: dict[str, str],
    cwd: Path,
    lifecycle: WindowsLifecycle,
    diagnostic_file: Path | None = None,
    new_console: bool = False,
) -> subprocess.Popen[Any]:
    return support._spawn(
        argv,
        environment=environment,
        cwd=cwd,
        lifecycle=lifecycle,
        diagnostic_file=diagnostic_file,
        new_console=new_console,
    )


def _drain_diagnostic(stream: Any, path: Path) -> None:
    return support._drain_diagnostic(stream, path)


def _diagnostic_category(path: Path) -> str:
    return support._diagnostic_category(path)


def _cleanup_category(error: BaseException) -> str:
    return support._cleanup_category(error)


def _server_endpoint_available(mcp_url: str, control_token: str) -> bool:
    return support._server_endpoint_available(mcp_url, control_token)


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


def write_artifact(evidence_dir: Path, name: str, payload: bytes) -> None:
    return support.write_artifact(evidence_dir, name, payload)


def _write_success(evidence_dir: Path) -> None:
    return support._write_success(evidence_dir)


def _prepare_terminal(context: Any) -> None:
    diagnostics_root = context.root / "diagnostics"
    diagnostics_root.mkdir()
    diagnostics = {
        role: diagnostics_root / f"{role}.stderr.log"
        for role in ("server", "server-restart", "agent", "agent-reconnect")
    }
    categories: list[tuple[str, str]] = []
    context.metadata["diagnostics"] = diagnostics
    context.metadata["diagnostic_categories"] = categories
    context.lifecycle.job = WindowsJob()

    def collect_diagnostics() -> None:
        for label, path in diagnostics.items():
            if path.exists():
                categories.append((label, _diagnostic_category(path)))

    context.lifecycle.add_cleanup(collect_diagnostics)
    context.lifecycle.add_cleanup(
        context.lifecycle.wait_for_diagnostics,
        label="diagnostics",
    )
    context.lifecycle.add_cleanup(
        context.lifecycle.close_diagnostic_streams,
        label="diagnostic-streams",
    )
    context.lifecycle.add_cleanup(
        lambda: context.lifecycle.job.terminate(
            processes=context.lifecycle.processes
        ),
        label="windows-job",
    )


def _assert_owned(context: Any, phase: str) -> None:
    if context.lifecycle.job.active_processes() < 2:
        if phase == "agent-start":
            raise WindowsE2EError(
                "Windows Job Object did not retain Server and Agent"
            )
        raise WindowsE2EError("Windows Job Object lost a restarted process")


def _report(
    context: Any,
    _scenario_error: BaseException | None,
    cleanup_error: BaseException | None,
) -> None:
    if context is None:
        return
    categories = context.metadata.get("diagnostic_categories", [])
    if categories:
        for label, category in categories:
            print(
                f"Windows E2E {label} diagnostics: {category}.",
                file=sys.stderr,
            )
    if cleanup_error is not None:
        print(
            "Windows E2E cleanup: "
            f"{_cleanup_category(cleanup_error)}.",
            file=sys.stderr,
        )


def _terminal_adapter() -> Any:
    def spawn(
        role: str,
        argv: Sequence[str],
        environment: dict[str, str],
        cwd: Path,
        context: Any,
    ) -> subprocess.Popen[Any]:
        diagnostics = context.metadata["diagnostics"]
        return _spawn(
            argv,
            environment=environment,
            cwd=cwd,
            lifecycle=context.lifecycle,
            diagnostic_file=diagnostics.get(role),
        )

    return harness.TerminalAdapter(
        label="Windows",
        device_id=DEVICE_ID,
        run_id_prefix="windows-",
        temp_prefix="agent-relay-windows-e2e-",
        success_message="Windows MCP end-to-end scenario passed.",
        failure_prefix="Windows E2E failed at scenario-",
        cleanup_message="Windows E2E cleanup failed",
        error_type=WindowsE2EError,
        lifecycle_factory=lambda: WindowsLifecycle(),
        minimal_environment=minimal_environment,
        create_workspace=_create_workspace,
        spawn=spawn,
        stop_process=lambda process, context: context.lifecycle.stop_process(process),
        status=_status,
        endpoint_available=_server_endpoint_available,
        assert_owned=_assert_owned,
        write_artifact=write_artifact,
        prepare=_prepare_terminal,
        report=_report,
        expected_workspace=lambda path: str(path.resolve(strict=True)),
    )


def run_scenario(
    evidence_dir: Path | None = None, *, output_file: Path | None = None
) -> None:
    """Run core MCP, offline detection, and real reconnect on Windows."""
    if os.name != "nt":
        raise WindowsE2EError("Windows harness requires Windows")
    harness.run_terminal_scenario(
        _terminal_adapter(), evidence_dir, output_file=output_file
    )


def main(argv: Sequence[str] | None = None) -> int:
    return harness.run_entrypoint("Windows Agent Relay E2E", run_scenario, argv)


if __name__ == "__main__":
    raise SystemExit(main())

"""Shared browser CUA harness over platform graphical-session primitives."""

from __future__ import annotations

import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol

from . import scenarios
from .chrome import find_chrome
from .common import (
    E2EError,
    agent_command,
    choose_loopback_port,
    generate_credentials,
    prepare_artifact_directory,
    server_command,
    write_artifact,
    write_success,
)
from .terminal import (
    AGENT_READY_TIMEOUT_SECONDS,
    SERVER_READY_TIMEOUT_SECONDS,
    ProcessHandle,
    ProcessPlatform,
    _create_workspace,
    _status,
    _wait_for,
)

CUA_CAPABILITIES = (
    "cua.browser_click",
    "cua.browser_navigate",
    "cua.browser_prepare",
    "cua.browser_type",
    "cua.click",
    "cua.end_session",
    "cua.get_browser_state",
    "cua.get_window_state",
    "cua.kill_app",
    "cua.launch_app",
    "cua.list_windows",
    "cua.start_session",
    "cua.type_text",
    "system.ping",
    "terminal.exec",
)
CUA_AGENT_TOOLS = (
    "relay_system_ping",
    "relay_terminal_exec",
    "relay_cua_list_windows",
    "relay_cua_get_window_state",
    "relay_cua_launch_app",
    "relay_cua_kill_app",
    "relay_cua_click",
    "relay_cua_type_text",
    "relay_cua_get_browser_state",
    "relay_cua_browser_prepare",
    "relay_cua_browser_navigate",
    "relay_cua_browser_click",
    "relay_cua_browser_type",
    "relay_cua_start_session",
    "relay_cua_end_session",
)
FIXTURE_READY_TIMEOUT_SECONDS = 15.0
FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "cua" / "server.py"


class GraphicalSession(Protocol):
    """Prepare only the OS graphical environment used by the shared runner."""

    def prepare(
        self,
        platform: ProcessPlatform,
        *,
        root: Path,
        home: Path,
        repository: Path,
    ) -> dict[str, str]: ...


def _fixture_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}health", timeout=2) as response:
            return response.read() == b'{"status":"ready"}'
    except (OSError, urllib.error.URLError):
        return False


def _write_failure(
    platform_name: str,
    phase: str,
    scenario_phase: list[str],
    error: BaseException,
    cleanup_error: BaseException | None,
    output_file: Path | None,
) -> None:
    detail = f": {error}" if isinstance(error, E2EError) else f": {type(error).__name__}"
    if scenario_phase:
        detail += f" (phase-{scenario_phase[-1]})"
    lines = [f"{platform_name} CUA E2E failed at scenario-{phase}{detail}."]
    print(lines[0], file=sys.stderr)
    if cleanup_error is not None:
        cleanup_line = f"{platform_name} CUA E2E cleanup failed."
        lines.append(cleanup_line)
        print(cleanup_line, file=sys.stderr)
    if output_file is not None:
        try:
            write_artifact(
                output_file.parent,
                output_file.name,
                ("\n".join(lines) + "\n").encode("ascii"),
            )
        except BaseException:
            print(
                f"{platform_name} CUA E2E artifact write failed.",
                file=sys.stderr,
            )


def run_cua_e2e(
    platform: ProcessPlatform,
    graphical_session: GraphicalSession,
    evidence_dir: Path | None = None,
    *,
    output_file: Path | None = None,
) -> None:
    """Run one canonical Chrome fixture scenario through the real public MCP path."""
    phase = "setup"
    scenario_phase: list[str] = []
    scenario_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    temporary: tempfile.TemporaryDirectory[str] | None = None
    processes: list[ProcessHandle] = []

    try:
        platform.prepare()
        agent_token, control_token = generate_credentials()
        server_port = choose_loopback_port()
        fixture_port = choose_loopback_port()
        run_id = f"{platform.cua_run_prefix}-{os.urandom(12).hex()}"
        value = f"relay-gh-cua-{run_id}"
        temporary = tempfile.TemporaryDirectory(
            prefix=f"agent-relay-{platform.cua_run_prefix}-"
        )
        root = Path(temporary.name)
        home = root / "home"
        workspace = root / "workspace"
        requested_artifacts = evidence_dir or root / "computer-evidence"
        local_artifacts = (
            requested_artifacts
            if requested_artifacts.is_absolute()
            else Path.cwd() / requested_artifacts
        )
        home.mkdir(parents=True, exist_ok=True)
        prepare_artifact_directory(local_artifacts)
        _create_workspace(workspace, home=home)
        event_artifact = local_artifacts / scenarios.CUA_EVENT_FILE
        if event_artifact.exists() or event_artifact.is_symlink():
            raise E2EError("computer oracle exists before the scenario")
        repository = Path(__file__).parents[2].resolve()
        graphical_environment = graphical_session.prepare(
            platform,
            root=root,
            home=home,
            repository=repository,
        )
        chrome = find_chrome()
        mcp_url = f"http://127.0.0.1:{server_port}/mcp"
        fixture_url = f"http://127.0.0.1:{fixture_port}/"
        server_environment = platform.minimal_environment(
            home,
            {
                "RELAY_SERVER_HOST": "127.0.0.1",
                "RELAY_SERVER_PORT": str(server_port),
                "RELAY_MCP_TOKEN": control_token,
                "RELAY_AGENT_TOKEN": agent_token,
            },
        )
        agent_environment = dict(graphical_environment)
        agent_environment.update(
            {
                "RELAY_URL": f"ws://127.0.0.1:{server_port}/ws/agent",
                "RELAY_AGENT_TOKEN": agent_token,
                "RELAY_AGENT_ID": platform.device_id,
                "RELAY_AGENT_WORKSPACE": str(workspace),
                "RELAY_AGENT_HEARTBEAT_INTERVAL_SECONDS": "0.2",
                "RELAY_AGENT_TOOLS": ",".join(CUA_AGENT_TOOLS),
                "RELAY_AGENT_E2E_RUN_ID": run_id,
                "RELAY_AGENT_COMPUTER_ACTION_TIMEOUT_SECONDS": "30",
                "RELAY_NATIVE_DEBUG": "1",
                "AGENT_RELAY_CUA_GRANT_EXISTING_PROFILE": "1",
                "CUA_DRIVER_TELEMETRY": "0",
                "CUA_DRIVER_RS_TELEMETRY_ENABLED": "0",
            }
        )
        fixture_environment = platform.minimal_environment(
            home,
            {"ARTIFACTS_DIR": str(local_artifacts)},
        )

        phase = "server-start"
        server = platform.spawn(
            server_command(server_port),
            environment=server_environment,
            cwd=repository,
            label="server",
        )
        processes.append(server)
        _wait_for(
            f"{platform.name} CUA server",
            lambda: _status(
                mcp_url,
                control_token,
                device_id=platform.device_id,
                connected=False,
                allow_unenrolled=True,
            )
            is None,
            timeout=SERVER_READY_TIMEOUT_SECONDS,
        )

        phase = "fixture-start"
        fixture = platform.spawn(
            [
                sys.executable,
                "-I",
                str(FIXTURE_SERVER),
                "--run-id",
                run_id,
                "--port",
                str(fixture_port),
            ],
            environment=fixture_environment,
            cwd=repository,
            label="fixture",
        )
        processes.append(fixture)
        _wait_for(
            f"{platform.name} CUA browser fixture",
            lambda: _fixture_ready(fixture_url),
            timeout=FIXTURE_READY_TIMEOUT_SECONDS,
        )

        phase = "agent-start"
        agent = platform.spawn(
            agent_command(server_port, workspace),
            environment=agent_environment,
            cwd=repository,
            label="agent",
        )
        processes.append(agent)

        def agent_ready() -> bool:
            if agent.poll() is not None:
                raise E2EError(f"{platform.name} CUA Agent exited during startup")
            _status(
                mcp_url,
                control_token,
                device_id=platform.device_id,
                connected=True,
                expected_capabilities=CUA_CAPABILITIES,
            )
            return True

        _wait_for(
            f"{platform.name} CUA Agent registration",
            agent_ready,
            timeout=AGENT_READY_TIMEOUT_SECONDS,
        )
        runtime = scenarios.RuntimeConfig(
            mcp_url=mcp_url,
            control_token=control_token,
            device_id=platform.device_id,
            run_id=run_id,
            fixture_url=fixture_url,
            fixtures_root=str(local_artifacts),
            browser_launch_path=str(chrome),
        )

        phase = "browser-scenario"
        scenarios.run_browser_scenario(
            runtime,
            value,
            scenario_phase,
            expected_capabilities=CUA_CAPABILITIES,
        )
        if any(process.poll() is not None for process in processes):
            raise E2EError(f"{platform.name} CUA owned process exited unexpectedly")
    except BaseException as error:
        scenario_error = error

    try:
        platform.cleanup()
    except BaseException as error:
        cleanup_error = error
    if temporary is not None:
        try:
            temporary.cleanup()
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = error

    primary_error = scenario_error or cleanup_error
    if primary_error is not None:
        _write_failure(
            platform.name,
            phase,
            scenario_phase,
            primary_error,
            cleanup_error,
            output_file,
        )
        raise primary_error

    success_line = f"{platform.name} CUA smoke scenario passed."
    if output_file is not None:
        write_artifact(
            output_file.parent,
            output_file.name,
            (success_line + "\n").encode("ascii"),
        )
    if evidence_dir is not None:
        write_success(evidence_dir)
    print(success_line)

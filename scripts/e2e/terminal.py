"""Shared Terminal E2E lifecycle and platform adapter composition."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from . import mcp_client, oracles, scenarios
from .common import (
    E2EError,
    agent_command,
    choose_loopback_port,
    generate_credentials,
    server_command,
    write_artifact,
    write_success,
)
from .harness import run_lifecycle

CORE_CAPABILITIES = ("system.ping", "terminal.exec")
POLL_INTERVAL_SECONDS = 0.1
SERVER_READY_TIMEOUT_SECONDS = 15.0
AGENT_READY_TIMEOUT_SECONDS = 30.0
FIXTURE_URL = "http://127.0.0.1:8899/"


class ProcessHandle(Protocol):
    pid: int
    returncode: int | None

    def poll(self) -> int | None: ...


class ProcessPlatform(Protocol):
    """OS primitives needed by the shared Terminal adapter."""

    name: str
    device_id: str
    run_prefix: str
    cua_run_prefix: str

    def prepare(self) -> None: ...

    def minimal_environment(
        self,
        home: Path,
        values: dict[str, str],
    ) -> dict[str, str]: ...

    def spawn(
        self,
        argv: Sequence[str],
        *,
        environment: dict[str, str],
        cwd: Path,
        label: str,
    ) -> ProcessHandle: ...

    def stop(self, process: ProcessHandle) -> None: ...

    def expected_pwd(self, workspace: Path) -> str: ...

    def cleanup(self) -> None: ...


def _wait_for(description: str, predicate: Any, *, timeout: float) -> None:
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("timeout must be positive")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (ConnectionError, OSError, ValueError):
            pass
        time.sleep(POLL_INTERVAL_SECONDS)
    raise E2EError(f"timed out waiting for {description}")


def _status(
    mcp_url: str,
    control_token: str,
    *,
    device_id: str,
    connected: bool,
    expected_capabilities: tuple[str, ...] | None = None,
    allow_unenrolled: bool = False,
) -> None:
    result = mcp_client.call_tool(
        mcp_url,
        control_token,
        "relay_device_status",
        {},
        http_timeout=1.0,
        operation_timeout=2.0,
    )
    oracles.validate_status(
        result,
        device_id=None if allow_unenrolled else device_id,
        connected=connected,
        expected_capabilities=expected_capabilities,
        allow_unenrolled=allow_unenrolled,
    )


def _server_endpoint_available(mcp_url: str, control_token: str) -> bool:
    try:
        result = mcp_client.call_tool(
            mcp_url,
            control_token,
            "relay_device_status",
            {},
            http_timeout=1.0,
            operation_timeout=2.0,
        )
    except ConnectionError:
        return False
    return getattr(result, "is_error", getattr(result, "isError", None)) is False


def _create_workspace(path: Path, *, home: Path) -> None:
    path.mkdir(mode=0o755)
    environment = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C",
    }
    subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=relay-e2e-marker", str(path)],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        timeout=10,
        shell=False,
    )
    marker = path / "marker.txt"
    marker.write_text("agent-only workspace\n", encoding="utf-8")
    os.chmod(marker, 0o444)


class _TerminalService:
    def __init__(self, adapter: TerminalAdapter, role: str) -> None:
        self._adapter = adapter
        self._role = role
        self._process: ProcessHandle | None = None
        self._starts = 0

    def start(self) -> None:
        self._starts += 1
        suffix = "initial" if self._starts == 1 else "restart" if self._role == "server" else "reconnect"
        self._adapter.phase = f"{self._role}-{suffix}"
        if self._role == "server":
            argv = server_command(self._adapter.port)
            environment = self._adapter.server_environment
        else:
            argv = agent_command(self._adapter.port, self._adapter.workspace)
            environment = self._adapter.agent_environment
        self._process = self._adapter.platform.spawn(
            argv,
            environment=environment,
            cwd=self._adapter.repository,
            label=f"{self._role}-{suffix}",
        )

    def wait_ready(self) -> None:
        process = self._require_process()
        self._adapter.phase = f"{self._role}-ready"

        def process_running() -> None:
            if process.poll() is not None:
                raise E2EError(f"{self._adapter.platform.name} {self._role} exited during readiness")

        if self._role == "server":
            if self._starts == 1:
                def ready() -> bool:
                    process_running()
                    _status(
                        self._adapter.mcp_url,
                        self._adapter.control_token,
                        device_id=self._adapter.platform.device_id,
                        connected=False,
                        allow_unenrolled=True,
                    )
                    return True
            else:
                def ready() -> bool:
                    process_running()
                    return _server_endpoint_available(
                        self._adapter.mcp_url,
                        self._adapter.control_token,
                    )
            timeout = SERVER_READY_TIMEOUT_SECONDS
        else:
            def ready() -> bool:
                process_running()
                _status(
                    self._adapter.mcp_url,
                    self._adapter.control_token,
                    device_id=self._adapter.platform.device_id,
                    connected=True,
                    expected_capabilities=CORE_CAPABILITIES,
                )
                return True
            timeout = AGENT_READY_TIMEOUT_SECONDS
        _wait_for(
            f"{self._adapter.platform.name} {self._role} readiness",
            ready,
            timeout=timeout,
        )

    def stop(self) -> None:
        self._adapter.phase = f"{self._role}-stop"
        self._adapter.platform.stop(self._require_process())

    def wait_stopped(self) -> None:
        process = self._require_process()
        if process.poll() is None:
            raise E2EError(f"{self._adapter.platform.name} {self._role} did not stop")
        if self._role == "server":
            _wait_for(
                f"{self._adapter.platform.name} server unavailable state",
                lambda: not _server_endpoint_available(
                    self._adapter.mcp_url,
                    self._adapter.control_token,
                ),
                timeout=SERVER_READY_TIMEOUT_SECONDS,
            )
        else:
            _wait_for(
                f"{self._adapter.platform.name} agent offline state",
                lambda: _status(
                    self._adapter.mcp_url,
                    self._adapter.control_token,
                    device_id=self._adapter.platform.device_id,
                    connected=False,
                )
                is None,
                timeout=AGENT_READY_TIMEOUT_SECONDS,
            )

    def _require_process(self) -> ProcessHandle:
        if self._process is None:
            raise E2EError(f"{self._role} process is unavailable")
        return self._process


class TerminalAdapter:
    """Cross-platform Terminal setup over a small OS process contract."""

    def __init__(self, platform: ProcessPlatform) -> None:
        self.platform = platform
        self.server = _TerminalService(self, "server")
        self.agent = _TerminalService(self, "agent")
        self.phase = "setup"
        self.scenario_phase: list[str] = []
        self.cleanup_error: BaseException | None = None
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self.repository = Path(__file__).parents[2].resolve()
        self.port = 0
        self.control_token = ""
        self.mcp_url = ""
        self.workspace = Path()
        self.server_environment: dict[str, str] = {}
        self.agent_environment: dict[str, str] = {}
        self.runtime: scenarios.RuntimeConfig | None = None

    def prepare(self) -> None:
        self.platform.prepare()
        agent_token, self.control_token = generate_credentials()
        self.port = choose_loopback_port()
        run_id = f"{self.platform.run_prefix}-{os.urandom(12).hex()}"
        self._temporary = tempfile.TemporaryDirectory(
            prefix=f"agent-relay-{self.platform.run_prefix}-e2e-"
        )
        root = Path(self._temporary.name)
        home = root / "home"
        self.workspace = root / "workspace"
        artifacts = root / "artifacts"
        home.mkdir()
        artifacts.mkdir()
        _create_workspace(self.workspace, home=home)
        self.mcp_url = f"http://127.0.0.1:{self.port}/mcp"
        self.server_environment = self.platform.minimal_environment(
            home,
            {
                "RELAY_SERVER_HOST": "127.0.0.1",
                "RELAY_SERVER_PORT": str(self.port),
                "RELAY_MCP_TOKEN": self.control_token,
                "RELAY_AGENT_TOKEN": agent_token,
            },
        )
        self.agent_environment = self.platform.minimal_environment(
            home,
            {
                "RELAY_URL": f"ws://127.0.0.1:{self.port}/ws/agent",
                "RELAY_AGENT_TOKEN": agent_token,
                "RELAY_AGENT_ID": self.platform.device_id,
                "RELAY_AGENT_WORKSPACE": str(self.workspace),
                "RELAY_AGENT_HEARTBEAT_INTERVAL_SECONDS": "0.2",
                "RELAY_AGENT_TOOLS": "relay_system_ping,relay_terminal_exec",
                "RELAY_AGENT_E2E_RUN_ID": run_id,
            },
        )
        self.runtime = scenarios.RuntimeConfig(
            mcp_url=self.mcp_url,
            control_token=self.control_token,
            device_id=self.platform.device_id,
            run_id=run_id,
            fixture_url=FIXTURE_URL,
            fixtures_root=str(artifacts),
        )

    def run_scenario(self) -> None:
        if self.runtime is None:
            raise E2EError("terminal runtime is unavailable")
        self.phase = "core-scenario"
        scenarios.run_core_scenario(
            self.runtime,
            self.scenario_phase,
            expected_capabilities=CORE_CAPABILITIES,
            expected_pwd=self.platform.expected_pwd(self.workspace),
        )

    def collect_evidence(self) -> None:
        self.phase = "evidence"

    def cleanup(self) -> None:
        failures: list[BaseException] = []
        try:
            self.platform.cleanup()
        except BaseException as error:
            failures.append(error)
        if self._temporary is not None:
            try:
                self._temporary.cleanup()
            except BaseException as error:
                failures.append(error)
        if failures:
            self.cleanup_error = E2EError(
                f"{self.platform.name} E2E cleanup failed"
            )
            self.cleanup_error.__cause__ = failures[0]
            raise self.cleanup_error


def run_terminal_e2e(
    platform: ProcessPlatform,
    evidence_dir: Path | None = None,
    *,
    output_file: Path | None = None,
) -> None:
    """Execute the canonical Terminal scenario through one platform manager."""
    adapter = TerminalAdapter(platform)
    try:
        run_lifecycle(adapter, adapter.run_scenario)
    except BaseException as error:
        detail = f": {error}" if isinstance(error, E2EError) else f": {type(error).__name__}"
        if adapter.scenario_phase:
            detail += f" (phase-{adapter.scenario_phase[-1]})"
        lines = [f"{platform.name} E2E failed at scenario-{adapter.phase}{detail}."]
        print(lines[0], file=sys.stderr)
        if adapter.cleanup_error is not None:
            cleanup_line = f"{platform.name} E2E cleanup failed."
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
                    f"{platform.name} E2E artifact write failed.",
                    file=sys.stderr,
                )
        raise

    success_line = f"{platform.name} MCP end-to-end scenario passed."
    if output_file is not None:
        write_artifact(
            output_file.parent,
            output_file.name,
            (success_line + "\n").encode("ascii"),
        )
    if evidence_dir is not None:
        write_success(evidence_dir)
    print(success_line)

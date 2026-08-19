#!/usr/bin/env python3
"""Shared orchestration for Agent Relay's native E2E entrypoints.

The platform modules provide process and fixture adapters.  This module owns the
ordered lifecycle that must stay identical across Linux and Windows: start the
server, wait for readiness, register the Agent, run the public scenario, verify
restart/reconnect behavior, publish bounded evidence, and clean up every owned
process.
"""

import argparse
import importlib.util
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from tests.e2e import mcp_client as portable_mcp
    from tests.e2e import oracles as portable_oracles
    from tests.e2e import scenarios as portable_scenarios
except ModuleNotFoundError as error:
    if error.name not in {"tests", "tests.e2e"}:
        raise

    def _load_portable(name: str) -> Any:
        dotted = f"_agent_relay_shared_e2e_{name}"
        cached = sys.modules.get(dotted)
        if cached is not None:
            return cached
        target = Path(__file__).parents[1] / "tests" / "e2e" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(dotted, target)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load portable kernel module {name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[dotted] = module
        spec.loader.exec_module(module)
        return module

    portable_mcp = _load_portable("mcp_client")
    portable_oracles = _load_portable("oracles")
    portable_scenarios = _load_portable("scenarios")


DEVICE_ID = "native-e2e-agent"
CORE_CAPABILITIES = ("system.ping", "terminal.exec")
FIXTURE_URL = "http://127.0.0.1:8899/"
POLL_INTERVAL_SECONDS = 0.1
SERVER_READY_TIMEOUT_SECONDS = 15.0
AGENT_READY_TIMEOUT_SECONDS = 30.0
PROCESS_STOP_TIMEOUT_SECONDS = 5.0
MAX_TOKEN_LENGTH = 128
SUCCESS_MARKER = b'{"status":"passed"}\n'


class E2EHarnessError(RuntimeError):
    """A bounded, non-sensitive native harness failure."""


def generate_credentials(error_type: type[RuntimeError] = E2EHarnessError) -> tuple[str, str]:
    """Generate distinct in-memory Agent and control credentials."""
    agent_token = secrets.token_urlsafe(48)
    control_token = secrets.token_urlsafe(48)
    if (
        not agent_token
        or not control_token
        or agent_token == control_token
        or len(agent_token) > MAX_TOKEN_LENGTH
        or len(control_token) > MAX_TOKEN_LENGTH
    ):
        raise error_type("ephemeral credential generation failed")
    return agent_token, control_token


def choose_loopback_port() -> int:
    """Reserve a currently unused loopback port for one native run."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _validate_port(port: int) -> None:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be a valid TCP port")


def server_command(port: int) -> list[str]:
    """Return the fixed Server argv used by both native harnesses."""
    _validate_port(port)
    installed_command = os.environ.get("RELAY_E2E_AGENT_RELAY_COMMAND")
    if installed_command:
        return [installed_command, "server"]
    return [
        sys.executable,
        "-m",
        "agent_relay.server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]


def agent_command(port: int, workspace: Path) -> list[str]:
    """Return the fixed Agent argv; runtime configuration is environment-only."""
    _validate_port(port)
    if not isinstance(workspace, Path) or not workspace.is_absolute():
        raise ValueError("workspace must be an absolute path")
    installed_command = os.environ.get("RELAY_E2E_AGENT_RELAY_COMMAND")
    if installed_command:
        return [installed_command, "agent"]
    return [sys.executable, "-m", "agent_relay.agent"]


def wait_for_process_exit(
    process: subprocess.Popen[Any] | None, *, timeout: float
) -> bool:
    """Wait for one owned process, refusing an unbounded or invalid timeout."""
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("timeout must be positive")
    if process is None:
        raise ValueError("process is required")
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


class Lifecycle:
    """Own callbacks and signal handlers while preserving primary failures."""

    def __init__(
        self,
        error_type: type[RuntimeError],
        cleanup_message: str,
    ) -> None:
        self.error_type = error_type
        self.cleanup_message = cleanup_message
        self._cleanups: list[Callable[[], None]] = []
        self._cleanup_labels: list[str] = []
        self.cleanup_error: BaseException | None = None
        self.cleanup_failures: list[str] = []
        self._previous_handlers: dict[signal.Signals, Any] = {}
        self._cleaned = False

    def install_signal_handlers(self) -> None:
        """Turn termination into controlled cleanup instead of orphaning children."""
        if self._previous_handlers:
            return

        def interrupted(signum: int, _frame: Any) -> None:
            raise KeyboardInterrupt(f"received signal {signum}")

        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                self._previous_handlers[signum] = signal.signal(signum, interrupted)
        except BaseException:
            for signum, handler in self._previous_handlers.items():
                try:
                    signal.signal(signum, handler)
                except BaseException:
                    pass
            self._previous_handlers.clear()
            raise

    def add_cleanup(self, cleanup: Callable[[], None], *, label: str | None = None) -> None:
        self._cleanups.append(cleanup)
        self._cleanup_labels.append(label or f"cleanup-{len(self._cleanups)}")

    def cleanup(self) -> None:
        if self._cleaned:
            return
        failures: list[BaseException] = []
        for signum in self._previous_handlers:
            try:
                signal.signal(signum, signal.SIG_IGN)
            except BaseException as error:
                failures.append(error)
                self.cleanup_failures.append("signal-handlers")
        for index in range(len(self._cleanups) - 1, -1, -1):
            try:
                self._cleanups[index]()
            except BaseException as error:  # cleanup must not stop remaining cleanup
                failures.append(error)
                self.cleanup_failures.append(self._cleanup_labels[index])
        for signum, handler in self._previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except BaseException as error:
                failures.append(error)
                self.cleanup_failures.append("signal-handlers")
        self._cleaned = True
        if failures:
            raise self.error_type(self.cleanup_message) from failures[0]

    def __enter__(self) -> "Lifecycle":
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: Any,
    ) -> bool:
        try:
            self.cleanup()
        except BaseException as cleanup_error:
            self.cleanup_error = cleanup_error
            if exception_type is None:
                raise
        return False


def wait_for(
    description: str,
    predicate: Callable[[], bool],
    *,
    timeout: float,
    error_type: type[RuntimeError] = E2EHarnessError,
) -> None:
    """Poll one readiness predicate with a bounded timeout."""
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("timeout must be positive")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (ConnectionError, OSError, ValueError) as error:
            if os.environ.get("RELAY_NATIVE_DEBUG") == "1":
                print(
                    f"readiness {description}: {type(error).__name__}",
                    file=sys.stderr,
                )
        time.sleep(POLL_INTERVAL_SECONDS)
    raise error_type(f"timed out waiting for {description}")


def status(
    mcp_url: str,
    control_token: str,
    *,
    device_id: str,
    connected: bool,
    expected_capabilities: tuple[str, ...] | None = None,
    allow_unenrolled: bool = False,
) -> None:
    result = portable_mcp.call_tool(
        mcp_url,
        control_token,
        "relay_device_status",
        {},
        http_timeout=1.0,
        operation_timeout=2.0,
    )
    portable_oracles.validate_status(
        result,
        device_id=None if allow_unenrolled else device_id,
        connected=connected,
        expected_capabilities=expected_capabilities,
        allow_unenrolled=allow_unenrolled,
    )


def server_endpoint_available(mcp_url: str, control_token: str) -> bool:
    """Probe MCP transport availability without exposing response data."""
    try:
        result = portable_mcp.call_tool(
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


def runtime_config(
    *,
    mcp_url: str,
    control_token: str,
    device_id: str,
    run_id: str,
    fixtures_root: Path,
    fixture_url: str = FIXTURE_URL,
    browser_pid: str = "",
    browser_launch_path: str = "",
) -> Any:
    return portable_scenarios.RuntimeConfig(
        mcp_url=mcp_url,
        control_token=control_token,
        device_id=device_id,
        run_id=run_id,
        fixture_url=fixture_url,
        fixtures_root=str(fixtures_root),
        browser_pid=browser_pid,
        browser_launch_path=browser_launch_path,
    )


def create_workspace(path: Path, *, readonly_marker: bool = False) -> None:
    path.mkdir(mode=0o755)
    subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=relay-e2e-marker", str(path)],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        shell=False,
    )
    marker = path / "marker.txt"
    marker.write_text("agent-only workspace\n", encoding="utf-8")
    if readonly_marker:
        os.chmod(marker, 0o444)


@dataclass
class TerminalContext:
    lifecycle: Lifecycle
    root: Path
    home: Path
    workspace: Path
    artifacts: Path
    repository: Path
    mcp_url: str
    runtime: Any
    server_environment: dict[str, str]
    agent_environment: dict[str, str]
    processes: list[subprocess.Popen[Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TerminalAdapter:
    """Small function bundle for one native process model."""

    label: str
    device_id: str
    run_id_prefix: str
    temp_prefix: str
    success_message: str
    failure_prefix: str
    cleanup_message: str
    error_type: type[RuntimeError]
    lifecycle_factory: Callable[[], Lifecycle]
    minimal_environment: Callable[[Path, dict[str, str]], dict[str, str]]
    create_workspace: Callable[[Path], None]
    spawn: Callable[..., subprocess.Popen[Any]]
    stop_process: Callable[..., None]
    status: Callable[..., None]
    endpoint_available: Callable[[str, str], bool]
    assert_owned: Callable[[TerminalContext, str], None]
    write_artifact: Callable[[Path, str, bytes], None]
    report: Callable[[TerminalContext | None, BaseException | None, BaseException | None], None]
    prepare: Callable[[TerminalContext], None] | None = None
    expected_workspace: Callable[[Path], str] = str


def _terminal_context(
    adapter: TerminalAdapter,
    lifecycle: Lifecycle,
    root: Path,
    *,
    agent_token: str,
    control_token: str,
    run_id: str,
    port: int,
) -> TerminalContext:
    home = root / "home"
    workspace = root / "workspace"
    artifacts = root / "artifacts"
    home.mkdir()
    artifacts.mkdir()
    adapter.create_workspace(workspace)
    repository = Path(__file__).parents[1].resolve()
    mcp_url = f"http://127.0.0.1:{port}/mcp"
    server_environment = adapter.minimal_environment(
        home,
        {
            "RELAY_SERVER_HOST": "127.0.0.1",
            "RELAY_SERVER_PORT": str(port),
            "RELAY_MCP_TOKEN": control_token,
            "RELAY_AGENT_TOKEN": agent_token,
        },
    )
    agent_environment = adapter.minimal_environment(
        home,
        {
            "RELAY_URL": f"ws://127.0.0.1:{port}/ws/agent",
            "RELAY_AGENT_TOKEN": agent_token,
            "RELAY_AGENT_ID": adapter.device_id,
            "RELAY_AGENT_WORKSPACE": str(workspace),
            "RELAY_AGENT_HEARTBEAT_INTERVAL_SECONDS": "0.2",
            "RELAY_AGENT_TOOLS": "relay_system_ping,relay_terminal_exec",
            "RELAY_AGENT_E2E_RUN_ID": run_id,
        },
    )
    runtime = runtime_config(
        mcp_url=mcp_url,
        control_token=control_token,
        device_id=adapter.device_id,
        run_id=run_id,
        fixtures_root=artifacts,
    )
    return TerminalContext(
        lifecycle=lifecycle,
        root=root,
        home=home,
        workspace=workspace,
        artifacts=artifacts,
        repository=repository,
        mcp_url=mcp_url,
        runtime=runtime,
        server_environment=server_environment,
        agent_environment=agent_environment,
    )


def _publish_success(
    adapter: "TerminalAdapter | CuaAdapter",
    evidence_dir: Path | None,
    output_file: Path | None,
) -> None:
    if output_file is not None:
        adapter.write_artifact(
            output_file.parent,
            output_file.name,
            f"{adapter.success_message}\n".encode("ascii"),
        )
    if evidence_dir is not None:
        adapter.write_artifact(evidence_dir, "success.json", SUCCESS_MARKER)


def _failure_line(
    adapter: "TerminalAdapter | CuaAdapter",
    phase: str,
    scenario_phase: list[str],
    error: BaseException,
) -> str:
    detail = (
        f": {error}"
        if isinstance(error, adapter.error_type)
        else f": {type(error).__name__}"
    )
    if scenario_phase:
        detail += f" (phase-{scenario_phase[-1]})"
    return f"{adapter.failure_prefix}{phase}{detail}."


def _publish_failure(
    adapter: "TerminalAdapter | CuaAdapter",
    output_file: Path | None,
    line: str,
    *,
    scenario_error: BaseException | None,
    cleanup_error: BaseException | None,
) -> None:
    if scenario_error is not None and cleanup_error is not None:
        line = f"{line}\n{adapter.cleanup_message}."
    if output_file is not None:
        try:
            adapter.write_artifact(
                output_file.parent,
                output_file.name,
                f"{line}\n".encode("ascii"),
            )
        except BaseException:
            print(f"{adapter.label} E2E artifact write failed.", file=sys.stderr)


def run_terminal_scenario(
    adapter: TerminalAdapter,
    evidence_dir: Path | None = None,
    *,
    output_file: Path | None = None,
) -> None:
    """Run the shared terminal lifecycle through a platform adapter."""
    adapter.lifecycle_factory  # keep adapter validation explicit for thin wrappers
    agent_token, control_token = generate_credentials(adapter.error_type)
    port = choose_loopback_port()
    run_id = f"{adapter.run_id_prefix}{secrets.token_hex(12)}"
    lifecycle = adapter.lifecycle_factory()
    phase = "setup"
    scenario_phase: list[str] = []
    scenario_error: BaseException | None = None
    context: TerminalContext | None = None

    try:
        lifecycle.install_signal_handlers()
        with lifecycle:
            temporary = tempfile.TemporaryDirectory(prefix=adapter.temp_prefix)
            lifecycle.add_cleanup(temporary.cleanup)
            context = _terminal_context(
                adapter,
                lifecycle,
                Path(temporary.name),
                agent_token=agent_token,
                control_token=control_token,
                run_id=run_id,
                port=port,
            )
            if adapter.prepare is not None:
                adapter.prepare(context)
            phase = "server-start"
            server = adapter.spawn(
                "server",
                server_command(port),
                context.server_environment,
                context.repository,
                context,
            )
            context.processes.append(server)
            wait_for(
                f"{adapter.label} server",
                lambda: (
                    adapter.status(
                        context.mcp_url,
                        control_token,
                        connected=False,
                        allow_unenrolled=True,
                    )
                    is None
                ),
                timeout=SERVER_READY_TIMEOUT_SECONDS,
                error_type=adapter.error_type,
            )
            if server.poll() is not None:
                raise adapter.error_type(
                    f"{adapter.label} server exited during startup ({server.returncode})"
                )

            phase = "agent-start"
            agent = adapter.spawn(
                "agent",
                agent_command(port, context.workspace),
                context.agent_environment,
                context.repository,
                context,
            )

            def agent_ready() -> bool:
                if agent.poll() is not None:
                    raise adapter.error_type(
                        f"{adapter.label} agent exited during startup"
                    )
                adapter.status(
                    context.mcp_url,
                    control_token,
                    connected=True,
                    expected_capabilities=CORE_CAPABILITIES,
                )
                return True

            wait_for(
                f"{adapter.label} agent registration",
                agent_ready,
                timeout=AGENT_READY_TIMEOUT_SECONDS,
                error_type=adapter.error_type,
            )
            if agent.poll() is not None:
                raise adapter.error_type(
                    f"{adapter.label} agent exited after registration"
                )
            adapter.assert_owned(context, "agent-start")

            phase = "core-scenario"
            portable_scenarios.run_core_scenario(
                context.runtime,
                scenario_phase,
                expected_capabilities=CORE_CAPABILITIES,
                expected_pwd=adapter.expected_workspace(context.workspace),
            )

            phase = "agent-stop"
            adapter.stop_process(agent, context)
            phase = "offline-detection"
            wait_for(
                f"{adapter.label} agent offline state",
                lambda: (
                    adapter.status(
                        context.mcp_url,
                        control_token,
                        connected=False,
                    )
                    is None
                ),
                timeout=AGENT_READY_TIMEOUT_SECONDS,
                error_type=adapter.error_type,
            )

            phase = "agent-reconnect"
            agent = adapter.spawn(
                "agent-reconnect",
                agent_command(port, context.workspace),
                context.agent_environment,
                context.repository,
                context,
            )
            wait_for(
                f"{adapter.label} agent reconnection",
                lambda: (
                    adapter.status(
                        context.mcp_url,
                        control_token,
                        connected=True,
                        expected_capabilities=CORE_CAPABILITIES,
                    )
                    is None
                ),
                timeout=AGENT_READY_TIMEOUT_SECONDS,
                error_type=adapter.error_type,
            )
            phase = "reconnected-core-scenario"
            portable_scenarios.run_core_scenario(
                context.runtime,
                scenario_phase,
                expected_capabilities=CORE_CAPABILITIES,
                expected_pwd=adapter.expected_workspace(context.workspace),
            )

            phase = "server-stop"
            adapter.stop_process(server, context)
            if server.poll() is None:
                raise adapter.error_type(f"{adapter.label} server did not stop")
            phase = "server-unavailable"
            wait_for(
                f"{adapter.label} server unavailable state",
                lambda: not adapter.endpoint_available(
                    context.mcp_url, control_token
                ),
                timeout=SERVER_READY_TIMEOUT_SECONDS,
                error_type=adapter.error_type,
            )

            phase = "server-restart"
            server = adapter.spawn(
                "server-restart",
                server_command(port),
                context.server_environment,
                context.repository,
                context,
            )
            wait_for(
                f"restarted {adapter.label} server",
                lambda: adapter.endpoint_available(
                    context.mcp_url, control_token
                ),
                timeout=SERVER_READY_TIMEOUT_SECONDS,
                error_type=adapter.error_type,
            )
            wait_for(
                f"{adapter.label} agent registration after server restart",
                lambda: (
                    adapter.status(
                        context.mcp_url,
                        control_token,
                        connected=True,
                        expected_capabilities=CORE_CAPABILITIES,
                    )
                    is None
                ),
                timeout=AGENT_READY_TIMEOUT_SECONDS,
                error_type=adapter.error_type,
            )
            phase = "post-restart-core-scenario"
            portable_scenarios.run_core_scenario(
                context.runtime,
                scenario_phase,
                expected_capabilities=CORE_CAPABILITIES,
                expected_pwd=adapter.expected_workspace(context.workspace),
            )
            if server.poll() is not None:
                raise adapter.error_type(
                    f"{adapter.label} server exited after restart"
                )
            adapter.assert_owned(context, "server-restart")
    except BaseException as error:
        scenario_error = error
        if not lifecycle._cleaned:
            try:
                lifecycle.cleanup()
            except BaseException as cleanup_error:
                lifecycle.cleanup_error = cleanup_error

    cleanup_error = lifecycle.cleanup_error
    adapter.report(context, scenario_error, cleanup_error)
    primary_error = scenario_error or cleanup_error
    if primary_error is None:
        try:
            _publish_success(adapter, evidence_dir, output_file)
        except BaseException as error:
            primary_error = error

    if primary_error is not None:
        line = _failure_line(adapter, phase, scenario_phase, primary_error)
        print(line, file=sys.stderr)
        if scenario_error is not None and cleanup_error is not None:
            print(f"{adapter.cleanup_message}.", file=sys.stderr)
        _publish_failure(
            adapter,
            output_file,
            line,
            scenario_error=scenario_error,
            cleanup_error=cleanup_error,
        )
        raise primary_error
    print(adapter.success_message)


@dataclass
class CuaContext:
    lifecycle: Lifecycle
    root: Path
    home: Path
    workspace: Path
    artifacts: Path
    repository: Path
    mcp_url: str
    runtime: Any
    value: str
    run_id: str
    phase: str = "setup"
    server: subprocess.Popen[Any] | None = None
    fixture: subprocess.Popen[Any] | None = None
    agent: subprocess.Popen[Any] | None = None
    processes: list[subprocess.Popen[Any]] = field(default_factory=list)
    diagnostics: dict[str, Path] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CuaAdapter:
    """Function bundle for platform-specific CUA setup and diagnostics."""

    label: str
    run_id_prefix: str
    temp_prefix: str
    success_message: str
    failure_prefix: str
    cleanup_message: str
    error_type: type[RuntimeError]
    lifecycle_factory: Callable[[], Lifecycle]
    write_artifact: Callable[[Path, str, bytes], None]
    validate_host: Callable[[], None]
    create_context: Callable[..., CuaContext]
    prepare_platform: Callable[[CuaContext], None]
    start_server: Callable[[CuaContext], subprocess.Popen[Any]]
    wait_server: Callable[[CuaContext], None]
    start_fixture: Callable[[CuaContext], subprocess.Popen[Any]]
    wait_fixture: Callable[[CuaContext], None]
    start_agent: Callable[[CuaContext], subprocess.Popen[Any]]
    wait_agent: Callable[[CuaContext], None]
    prepare_scenario: Callable[[CuaContext], None]
    run_scenario: Callable[[CuaContext, list[str]], None]
    assert_processes: Callable[[CuaContext], None]
    report_failure: Callable[[CuaContext, BaseException], None]
    report_after_cleanup: Callable[[CuaContext | None, BaseException | None, BaseException | None], None]
    failure_phase: Callable[[CuaContext | None, list[str], str], str] = (
        lambda _context, _phase, phase: phase
    )


def run_cua_scenario(
    adapter: CuaAdapter,
    evidence_dir: Path | None = None,
    *,
    output_file: Path | None = None,
) -> None:
    """Run the shared Server/Agent/CUA lifecycle through an adapter."""
    adapter.validate_host()
    agent_token, control_token = generate_credentials(adapter.error_type)
    run_id = f"{adapter.run_id_prefix}{secrets.token_hex(12)}"
    value = f"relay-gh-cua-{run_id}"
    lifecycle = adapter.lifecycle_factory()
    phase = "setup"
    scenario_phase: list[str] = []
    scenario_error: BaseException | None = None
    context: CuaContext | None = None

    try:
        lifecycle.install_signal_handlers()
        with lifecycle:
            temporary = tempfile.TemporaryDirectory(prefix=adapter.temp_prefix)
            lifecycle.add_cleanup(temporary.cleanup)
            context = adapter.create_context(
                Path(temporary.name),
                evidence_dir,
                agent_token,
                control_token,
                run_id,
                value,
                lifecycle,
            )
            context.phase = "platform-setup"
            adapter.prepare_platform(context)
            phase = context.phase

            phase = "server-start"
            context.phase = phase
            context.server = adapter.start_server(context)
            context.processes.append(context.server)
            adapter.wait_server(context)

            phase = "fixture-start"
            context.phase = phase
            context.fixture = adapter.start_fixture(context)
            context.processes.append(context.fixture)
            adapter.wait_fixture(context)

            phase = "agent-start"
            context.phase = phase
            context.agent = adapter.start_agent(context)
            context.processes.append(context.agent)
            adapter.wait_agent(context)

            phase = "computer-scenario"
            context.phase = phase
            adapter.prepare_scenario(context)
            phase = context.phase
            adapter.run_scenario(context, scenario_phase)
            adapter.assert_processes(context)
    except BaseException as error:
        scenario_error = error
        if context is not None:
            context.phase = adapter.failure_phase(context, scenario_phase, context.phase)
            phase = context.phase
            adapter.report_failure(context, error)
        if not lifecycle._cleaned:
            try:
                lifecycle.cleanup()
            except BaseException as cleanup_error:
                lifecycle.cleanup_error = cleanup_error

    cleanup_error = lifecycle.cleanup_error
    adapter.report_after_cleanup(context, scenario_error, cleanup_error)
    primary_error = scenario_error or cleanup_error
    if primary_error is None:
        try:
            _publish_success(adapter, evidence_dir, output_file)
        except BaseException as error:
            primary_error = error

    if primary_error is not None:
        if context is not None:
            phase = context.phase
        line = _failure_line(adapter, phase, scenario_phase, primary_error)
        print(line, file=sys.stderr)
        if scenario_error is not None and cleanup_error is not None:
            print(f"{adapter.cleanup_message}.", file=sys.stderr)
        _publish_failure(
            adapter,
            output_file,
            line,
            scenario_error=scenario_error,
            cleanup_error=cleanup_error,
        )
        raise primary_error
    print(adapter.success_message)


def run_entrypoint(
    description: str,
    runner: Callable[..., None],
    argv: Sequence[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--output-file", type=Path)
    args = parser.parse_args(argv)
    try:
        runner(args.evidence_dir, output_file=args.output_file)
    except BaseException:
        return 1
    return 0

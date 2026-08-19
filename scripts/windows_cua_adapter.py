#!/usr/bin/env python3
"""Windows Computer Use adapter for the shared native E2E harness."""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any


def _load_module(name: str, path: Path) -> Any:
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


windows = _load_module(
    "_agent_relay_windows_e2e_adapter",
    Path(__file__).with_name("windows_e2e_adapter.py"),
)
harness = windows.harness
portable_mcp = harness.portable_mcp
portable_scenarios = harness.portable_scenarios

ROOT = Path(__file__).parents[1].resolve()
FIXTURE = ROOT / "scripts" / "windows_computer_use_fixture.ps1"
DEVICE_ID = "windows-cua-e2e-agent"
COMPUTER_APP_NAME = "powershell"
COMPUTER_WINDOW_TITLE = "Agent Relay Computer Use Windows Fixture"
CUA_CAPABILITIES = (
    "cua.click",
    "cua.get_window_state",
    "cua.list_windows",
    "cua.type_text",
    "system.ping",
    "terminal.exec",
)
FIXTURE_READY_TIMEOUT_SECONDS = 15.0
AGENT_READY_TIMEOUT_SECONDS = 30.0
MAX_DIAGNOSTIC_BYTES = 16 * 1024


WindowsCuaE2EError = windows.WindowsE2EError


def _current_session_id() -> int:
    if os.name != "nt":
        raise WindowsCuaE2EError("Windows CUA harness requires Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.ProcessIdToSessionId.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.ProcessIdToSessionId.restype = ctypes.c_int
    session_id = ctypes.c_uint32()
    if not kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session_id)):
        raise WindowsCuaE2EError("could not inspect Windows session")
    return int(session_id.value)


def _powershell_executable() -> Path:
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if not root:
        raise WindowsCuaE2EError("Windows system root is unavailable")
    path = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not path.is_file() or path.is_symlink():
        raise WindowsCuaE2EError("Windows PowerShell executable is unavailable")
    return path


def _runtime(*, mcp_url: str, control_token: str, run_id: str, fixtures_root: Path) -> Any:
    return portable_scenarios.RuntimeConfig(
        mcp_url=mcp_url,
        control_token=control_token,
        device_id=DEVICE_ID,
        run_id=run_id,
        fixture_url="http://127.0.0.1:1/",
        fixtures_root=str(fixtures_root),
    )


def _status(
    mcp_url: str,
    control_token: str,
    *,
    connected: bool,
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
    try:
        windows.portable_oracles.validate_status(
            result,
            device_id=None if allow_unenrolled else DEVICE_ID,
            connected=connected,
            expected_capabilities=CUA_CAPABILITIES,
            allow_unenrolled=allow_unenrolled,
        )
    except ValueError:
        if os.environ.get("RELAY_NATIVE_DEBUG") == "1":
            print(
                "Windows CUA status diagnostic: "
                f"category={windows.portable_oracles.classify_status_failure(result, device_id=None if allow_unenrolled else DEVICE_ID, connected=connected, expected_capabilities=CUA_CAPABILITIES)}",
                file=sys.stderr,
                flush=True,
            )
        raise


def _fixture_ready(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        type(payload) is dict
        and set(payload) == {"kind", "title"}
        and payload.get("kind") == "ready"
        and payload.get("title") == COMPUTER_WINDOW_TITLE
    )


def _driver_environment(values: dict[str, str]) -> dict[str, str]:
    environment = windows.minimal_environment(Path(values["HOME"]), values)
    for name in ("CUA_DRIVER_RS_HOME",):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    environment["CUA_DRIVER_TELEMETRY"] = "0"
    environment["CUA_DRIVER_RS_TELEMETRY_ENABLED"] = "0"
    return environment


def _fixture_command(event_path: Path, ready_path: Path, run_id: str) -> list[str]:
    return [
        str(_powershell_executable()),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-STA",
        "-WindowStyle",
        "Normal",
        "-File",
        str(FIXTURE),
        "-EventPath",
        str(event_path),
        "-ReadyPath",
        str(ready_path),
        "-Title",
        COMPUTER_WINDOW_TITLE,
        "-RunId",
        run_id,
    ]



def validate_host() -> None:
    if os.name != "nt":
        raise WindowsCuaE2EError("Windows CUA harness requires Windows")
    if _current_session_id() == 0:
        raise WindowsCuaE2EError("Windows runner is in Session 0")


def create_context(
    root: Path,
    evidence_dir: Path | None,
    agent_token: str,
    control_token: str,
    run_id: str,
    value: str,
    lifecycle: Any,
) -> Any:
    server_port = windows.choose_loopback_port()
    home = root / "home"
    workspace = root / "workspace"
    local_artifacts = evidence_dir or root / "computer-evidence"
    diagnostics_root = root / "diagnostics"
    for path in (home, local_artifacts, diagnostics_root):
        path.mkdir(parents=True, exist_ok=True)
    windows._create_workspace(workspace)
    event_path = local_artifacts / "computer-events.jsonl"
    ready_path = root / "fixture-ready.json"
    if event_path.exists() or event_path.is_symlink():
        raise WindowsCuaE2EError("computer oracle exists before the scenario")
    lifecycle.job = windows.WindowsJob()
    mcp_url = f"http://127.0.0.1:{server_port}/mcp"
    server_environment = _driver_environment(
        {
            "HOME": str(home),
            "RELAY_SERVER_HOST": "127.0.0.1",
            "RELAY_SERVER_PORT": str(server_port),
            "RELAY_MCP_TOKEN": control_token,
            "RELAY_AGENT_TOKEN": agent_token,
        }
    )
    agent_environment = _driver_environment(
        {
            "HOME": str(home),
            "RELAY_URL": f"ws://127.0.0.1:{server_port}/ws/agent",
            "RELAY_AGENT_TOKEN": agent_token,
            "RELAY_AGENT_ID": DEVICE_ID,
            "RELAY_AGENT_WORKSPACE": str(workspace),
            "RELAY_AGENT_HEARTBEAT_INTERVAL_SECONDS": "0.2",
            "RELAY_AGENT_TOOLS": ",".join(
                (
                    "relay_system_ping",
                    "relay_terminal_exec",
                    "relay_cua_list_windows",
                    "relay_cua_get_window_state",
                    "relay_cua_click",
                    "relay_cua_type_text",
                )
            ),
            "RELAY_AGENT_COMPUTER_ALLOWED_APP_NAME": COMPUTER_APP_NAME,
            "RELAY_AGENT_COMPUTER_ALLOWED_WINDOW_TITLE": COMPUTER_WINDOW_TITLE,
            "RELAY_NATIVE_DEBUG": "1",
        }
    )
    fixture_environment = _driver_environment({"HOME": str(home)})
    runtime = _runtime(
        mcp_url=mcp_url,
        control_token=control_token,
        run_id=run_id,
        fixtures_root=local_artifacts,
    )
    diagnostics = {
        "server": diagnostics_root / "server.stderr.log",
        "fixture": diagnostics_root / "fixture.stderr.log",
        "agent": diagnostics_root / "agent.stderr.log",
    }
    categories: list[tuple[str, str]] = []

    def collect_diagnostics() -> None:
        for label, path in diagnostics.items():
            if path.exists():
                categories.append((label, windows._diagnostic_category(path)))

    lifecycle.add_cleanup(collect_diagnostics, label="collect-diagnostics")
    lifecycle.add_cleanup(lifecycle.wait_for_diagnostics, label="diagnostics")
    lifecycle.add_cleanup(
        lifecycle.close_diagnostic_streams,
        label="diagnostic-streams",
    )
    lifecycle.add_cleanup(
        lambda: lifecycle.job.terminate(processes=lifecycle.processes),
        label="windows-job",
    )
    context = harness.CuaContext(
        lifecycle=lifecycle,
        root=root,
        home=home,
        workspace=workspace,
        artifacts=local_artifacts,
        repository=ROOT,
        mcp_url=mcp_url,
        runtime=runtime,
        value=value,
        run_id=run_id,
        diagnostics=diagnostics,
    )
    context.metadata.update(
        {
            "server_port": server_port,
            "server_environment": server_environment,
            "agent_environment": agent_environment,
            "fixture_environment": fixture_environment,
            "event_path": event_path,
            "ready_path": ready_path,
            "diagnostic_categories": categories,
        }
    )
    return context


def prepare_platform(_context: Any) -> None:
    return None


def start_server(context: Any) -> Any:
    return windows._spawn(
        windows.server_command(context.metadata["server_port"]),
        environment=context.metadata["server_environment"],
        cwd=context.repository,
        lifecycle=context.lifecycle,
        diagnostic_file=context.diagnostics["server"],
    )


def wait_server(context: Any) -> None:
    windows._wait_for(
        "Windows CUA server",
        lambda: windows._server_endpoint_available(
            context.mcp_url, context.runtime.control_token
        ),
        timeout=windows.SERVER_READY_TIMEOUT_SECONDS,
    )


def start_fixture(context: Any) -> Any:
    return windows._spawn(
        _fixture_command(
            context.metadata["event_path"],
            context.metadata["ready_path"],
            context.run_id,
        ),
        environment=context.metadata["fixture_environment"],
        cwd=context.repository,
        lifecycle=context.lifecycle,
        diagnostic_file=context.diagnostics["fixture"],
        new_console=True,
    )


def wait_fixture(context: Any) -> None:
    readiness_kind: str | None = None

    def ready() -> bool:
        nonlocal readiness_kind
        if context.fixture is not None and context.fixture.poll() is not None:
            raise WindowsCuaE2EError("Windows CUA fixture exited before readiness")
        path = context.metadata["ready_path"]
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                kind = payload.get("kind")
                if kind in {"starting", "ready", "failed"}:
                    readiness_kind = kind
                if kind == "failed":
                    raise WindowsCuaE2EError(
                        "Windows CUA fixture reported failed readiness"
                    )
        return _fixture_ready(path)

    try:
        windows._wait_for(
            "Windows CUA fixture",
            ready,
            timeout=FIXTURE_READY_TIMEOUT_SECONDS,
        )
    except WindowsCuaE2EError as error:
        if readiness_kind is not None and readiness_kind != "ready":
            raise WindowsCuaE2EError(
                f"Windows CUA fixture readiness stopped at {readiness_kind}"
            ) from error
        raise


def start_agent(context: Any) -> Any:
    return windows._spawn(
        windows.agent_command(context.metadata["server_port"], context.workspace),
        environment=context.metadata["agent_environment"],
        cwd=context.repository,
        lifecycle=context.lifecycle,
        diagnostic_file=context.diagnostics["agent"],
    )


def wait_agent(context: Any) -> None:
    def ready() -> bool:
        if context.agent is not None and context.agent.poll() is not None:
            raise WindowsCuaE2EError("Windows CUA Agent exited during startup")
        _status(context.mcp_url, context.runtime.control_token, connected=True)
        return True

    windows._wait_for(
        "Windows CUA Agent registration",
        ready,
        timeout=AGENT_READY_TIMEOUT_SECONDS,
    )


def prepare_scenario(_context: Any) -> None:
    return None


def run_scenario(context: Any, phase: list[str]) -> None:
    context.metadata["scenario_phase"] = phase
    scenario_stop = threading.Event()

    def report_scenario_phase() -> None:
        last_marker = ""
        while not scenario_stop.wait(5.0):
            marker = phase[0]
            if marker != last_marker:
                print(f"Windows CUA scenario phase: {marker}", flush=True)
                last_marker = marker

    reporter = threading.Thread(target=report_scenario_phase, daemon=True)
    reporter.start()
    try:
        portable_scenarios.run_cua_scenario(
            context.runtime,
            context.value,
            phase,
            expected_capabilities=CUA_CAPABILITIES,
            expected_cua_app=COMPUTER_APP_NAME,
            expected_cua_window_title=COMPUTER_WINDOW_TITLE,
        )
    finally:
        scenario_stop.set()
        reporter.join(timeout=1.0)


def assert_processes(context: Any) -> None:
    if any(
        process.poll() is not None
        for process in (context.server, context.fixture, context.agent)
        if process is not None
    ):
        raise WindowsCuaE2EError("Windows CUA owned process exited unexpectedly")


def report_failure(_context: Any, _error: BaseException) -> None:
    return None


def report_after_cleanup(
    context: Any,
    scenario_error: BaseException | None,
    _cleanup_error: BaseException | None,
) -> None:
    if scenario_error is None or context is None:
        return
    for label, category in context.metadata["diagnostic_categories"]:
        print(f"Windows CUA {label} diagnostic: {category}", file=sys.stderr)


def failure_phase(context: Any, phase: list[str], current: str) -> str:
    if current == "computer-scenario" and phase:
        return f"computer-{phase[0]}"
    return current


def write_artifact(evidence_dir: Path, name: str, payload: bytes) -> None:
    windows.write_artifact(evidence_dir, name, payload)


def make_adapter() -> Any:
    return harness.CuaAdapter(
        label="Windows CUA",
        run_id_prefix="windows-cua-",
        temp_prefix="agent-relay-windows-cua-",
        success_message="Windows CUA smoke scenario passed.",
        failure_prefix="Windows CUA E2E failed at scenario-",
        cleanup_message="Windows CUA E2E cleanup failed",
        error_type=WindowsCuaE2EError,
        lifecycle_factory=lambda: windows.WindowsLifecycle(),
        write_artifact=write_artifact,
        validate_host=validate_host,
        create_context=create_context,
        prepare_platform=prepare_platform,
        start_server=start_server,
        wait_server=wait_server,
        start_fixture=start_fixture,
        wait_fixture=wait_fixture,
        start_agent=start_agent,
        wait_agent=wait_agent,
        prepare_scenario=prepare_scenario,
        run_scenario=run_scenario,
        assert_processes=assert_processes,
        report_failure=report_failure,
        report_after_cleanup=report_after_cleanup,
        failure_phase=failure_phase,
    )

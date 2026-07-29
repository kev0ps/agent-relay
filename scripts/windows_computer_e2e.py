#!/usr/bin/env python3
"""Run the native Windows Computer Use Agent Relay E2E scenario."""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import json
import os
import secrets
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).parents[1].resolve()
FIXTURE = ROOT / "scripts" / "windows_computer_use_fixture.ps1"
DEVICE_ID = "windows-cua-e2e-agent"
COMPUTER_APP_NAME = "powershell"
COMPUTER_WINDOW_TITLE = "Agent Relay Computer Use Windows Fixture"
CUA_CAPABILITIES = (
    "computer.capture",
    "computer.click",
    "computer.type",
    "system.ping",
    "terminal.exec",
)
FIXTURE_READY_TIMEOUT_SECONDS = 15.0
AGENT_READY_TIMEOUT_SECONDS = 30.0
MAX_DIAGNOSTIC_BYTES = 16 * 1024


def _load_windows_harness() -> Any:
    name = "_agent_relay_windows_e2e_support"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    path = Path(__file__).with_name("windows_e2e.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load Windows E2E support")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


windows = _load_windows_harness()
try:
    from tests.e2e import mcp_client as portable_mcp
    from tests.e2e import scenarios as portable_scenarios
except ModuleNotFoundError as error:
    if error.name not in {"tests", "tests.e2e"}:
        raise
    portable_mcp = windows._load_portable("mcp_client")
    portable_scenarios = windows._load_portable("scenarios")

WindowsCuaE2EError = windows.WindowsE2EError


def _current_session_id() -> int:
    if os.name != "nt":
        raise WindowsCuaE2EError("native Windows CUA harness requires Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.ProcessIdToSessionId.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.ProcessIdToSessionId.restype = ctypes.c_int
    session_id = ctypes.c_uint32()
    if not kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session_id)):
        raise WindowsCuaE2EError("could not inspect Windows session")
    return int(session_id.value)


def _resolve_driver() -> Path:
    raw = os.environ.get("AGENT_RELAY_COMPUTER_DRIVER_PATH")
    if not raw:
        raise WindowsCuaE2EError("cua-driver path is unavailable")
    path = Path(raw)
    if (
        not path.is_absolute()
        or path.suffix.casefold() != ".exe"
        or not path.is_file()
        or path.is_symlink()
        or not os.access(path, os.X_OK)
    ):
        raise WindowsCuaE2EError("cua-driver executable is invalid")
    return path


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


def _status(mcp_url: str, control_token: str, *, connected: bool) -> None:
    result = portable_mcp.call_tool(
        mcp_url,
        control_token,
        "relay_device_status",
        {},
        http_timeout=1.0,
        operation_timeout=2.0,
    )
    windows.portable_oracles.validate_status(
        result,
        device_id=DEVICE_ID,
        connected=connected,
        expected_capabilities=CUA_CAPABILITIES,
    )


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
    for name in ("CUA_DRIVER_RS_HOME", "CUA_DRIVER_RS_INSTALL_DIR"):
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


def run_scenario(
    evidence_dir: Path | None = None, *, output_file: Path | None = None
) -> None:
    """Run Server + Agent + WinForms fixture through public MCP Computer Use."""
    if os.name != "nt":
        raise WindowsCuaE2EError("native Windows CUA harness requires Windows")
    if _current_session_id() == 0:
        raise WindowsCuaE2EError("Windows runner is in Session 0")

    agent_token, control_token = windows.generate_credentials()
    server_port = windows.choose_loopback_port()
    run_id = f"windows-cua-{secrets.token_hex(12)}"
    value = f"relay-gh-cua-{run_id}"
    phase = "setup"
    scenario_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    lifecycle = windows.WindowsLifecycle()
    temporary: tempfile.TemporaryDirectory[str] | None = None
    diagnostics: dict[str, Path] = {}
    diagnostic_categories: list[tuple[str, str]] = []
    processes: list[Any] = []

    try:
        lifecycle.install_signal_handlers()
        temporary = tempfile.TemporaryDirectory(prefix="agent-relay-windows-cua-")
        lifecycle.add_cleanup(temporary.cleanup)
        root = Path(temporary.name)
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
        repository = ROOT
        driver = _resolve_driver()
        mcp_url = f"http://127.0.0.1:{server_port}/mcp"
        server_environment = _driver_environment(
            {
                "HOME": str(home),
                "AGENT_RELAY_DEVICE_ID": DEVICE_ID,
                "AGENT_RELAY_AGENT_TOKEN": agent_token,
                "AGENT_RELAY_CONTROL_TOKEN": control_token,
                "AGENT_RELAY_PORT": str(server_port),
            }
        )
        agent_environment = _driver_environment(
            {
                "HOME": str(home),
                "AGENT_RELAY_DEVICE_ID": DEVICE_ID,
                "AGENT_RELAY_AGENT_TOKEN": agent_token,
                "AGENT_RELAY_SERVER_URL": f"ws://127.0.0.1:{server_port}/ws/agent",
                "AGENT_RELAY_WORKSPACE": str(workspace),
                "AGENT_RELAY_HEARTBEAT_INTERVAL_SECONDS": "0.2",
                "AGENT_RELAY_COMPUTER_DRIVER_PATH": str(driver),
                "AGENT_RELAY_COMPUTER_ALLOWED_APP_NAME": COMPUTER_APP_NAME,
                "AGENT_RELAY_COMPUTER_ALLOWED_WINDOW_TITLE": COMPUTER_WINDOW_TITLE,
                "AGENT_RELAY_NATIVE_DEBUG": "1",
            }
        )
        fixture_environment = _driver_environment({"HOME": str(home)})
        runtime = _runtime(
            mcp_url=mcp_url,
            control_token=control_token,
            run_id=run_id,
            fixtures_root=local_artifacts,
        )
        diagnostics.update(
            {
                "server": diagnostics_root / "server.stderr.log",
                "fixture": diagnostics_root / "fixture.stderr.log",
                "agent": diagnostics_root / "agent.stderr.log",
            }
        )

        def collect_diagnostics() -> None:
            for label, path in diagnostics.items():
                if path.exists():
                    diagnostic_categories.append(
                        (label, windows._diagnostic_category(path))
                    )

        lifecycle.add_cleanup(collect_diagnostics, label="collect-diagnostics")
        lifecycle.add_cleanup(lifecycle.wait_for_diagnostics, label="diagnostics")
        lifecycle.add_cleanup(
            lifecycle.close_diagnostic_streams,
            label="diagnostic-streams",
        )
        lifecycle.add_cleanup(
            lambda: lifecycle.job.terminate(processes=processes), label="windows-job"
        )

        phase = "server-start"
        server = windows._spawn(
            windows.server_command(server_port),
            environment=server_environment,
            cwd=repository,
            lifecycle=lifecycle,
            diagnostic_file=diagnostics_root / "server.stderr.log",
        )
        processes.append(server)
        windows._wait_for(
            "native Windows CUA server",
            lambda: windows._server_endpoint_available(mcp_url, control_token),
            timeout=windows.SERVER_READY_TIMEOUT_SECONDS,
        )

        phase = "fixture-start"
        fixture = windows._spawn(
            _fixture_command(event_path, ready_path, run_id),
            environment=fixture_environment,
            cwd=repository,
            lifecycle=lifecycle,
            diagnostic_file=diagnostics_root / "fixture.stderr.log",
            new_console=True,
        )
        processes.append(fixture)

        readiness_kind: str | None = None

        def fixture_ready() -> bool:
            nonlocal readiness_kind
            if fixture.poll() is not None:
                raise WindowsCuaE2EError(
                    "Windows CUA fixture exited before readiness"
                )
            if ready_path.exists():
                try:
                    payload = json.loads(ready_path.read_text(encoding="utf-8"))
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
            return _fixture_ready(ready_path)

        try:
            windows._wait_for(
                "native Windows CUA fixture",
                fixture_ready,
                timeout=FIXTURE_READY_TIMEOUT_SECONDS,
            )
        except WindowsCuaE2EError as error:
            if readiness_kind is not None and readiness_kind != "ready":
                raise WindowsCuaE2EError(
                    f"Windows CUA fixture readiness stopped at {readiness_kind}"
                ) from error
            raise

        phase = "agent-start"
        agent = windows._spawn(
            windows.agent_command(server_port, workspace),
            environment=agent_environment,
            cwd=repository,
            lifecycle=lifecycle,
            diagnostic_file=diagnostics_root / "agent.stderr.log",
        )
        processes.append(agent)

        def agent_ready() -> bool:
            if agent.poll() is not None:
                raise WindowsCuaE2EError("Windows CUA Agent exited during startup")
            _status(mcp_url, control_token, connected=True)
            return True

        windows._wait_for(
            "native Windows CUA Agent registration",
            agent_ready,
            timeout=AGENT_READY_TIMEOUT_SECONDS,
        )
        phase = "computer-scenario"
        scenario_markers: list[str] = ["start"]
        scenario_stop = threading.Event()

        def report_scenario_phase() -> None:
            last_marker = ""
            while not scenario_stop.wait(5.0):
                marker = scenario_markers[0]
                if marker != last_marker:
                    print(f"Windows CUA scenario phase: {marker}", flush=True)
                    last_marker = marker

        reporter = threading.Thread(target=report_scenario_phase, daemon=True)
        reporter.start()
        try:
            portable_scenarios.run_computer_scenario(
                runtime,
                value,
                scenario_markers,
                expected_capabilities=CUA_CAPABILITIES,
                expected_computer_app=COMPUTER_APP_NAME,
                expected_computer_window_title=COMPUTER_WINDOW_TITLE,
            )
        finally:
            scenario_stop.set()
            reporter.join(timeout=1.0)
        if any(process.poll() is not None for process in processes):
            raise WindowsCuaE2EError("Windows CUA owned process exited unexpectedly")
    except BaseException as error:
        scenario_error = error
        if phase == "computer-scenario" and scenario_markers:
            phase = f"computer-{scenario_markers[0]}"

    if not lifecycle._cleaned:
        try:
            lifecycle.cleanup()
        except BaseException as error:
            cleanup_error = error

    if scenario_error is not None:
        for label, category in diagnostic_categories:
            print(f"Windows CUA {label} diagnostic: {category}", file=sys.stderr)

    primary_error = scenario_error or cleanup_error
    if primary_error is None:
        try:
            if output_file is not None:
                windows.write_artifact(
                    output_file.parent,
                    output_file.name,
                    b"Windows CUA smoke scenario passed.\n",
                )
            if evidence_dir is not None:
                windows._write_success(evidence_dir)
        except BaseException as error:
            primary_error = error

    if primary_error is not None:
        detail = (
            f": {primary_error}"
            if isinstance(primary_error, WindowsCuaE2EError)
            else f": {type(primary_error).__name__}"
        )
        line = f"Windows CUA E2E failed at scenario-{phase}{detail}."
        print(line, file=sys.stderr)
        if scenario_error is not None and cleanup_error is not None:
            print("Windows CUA E2E cleanup failed.", file=sys.stderr)
        if output_file is not None:
            try:
                windows.write_artifact(output_file.parent, output_file.name, (line + "\n").encode("ascii"))
            except BaseException:
                print("Windows CUA E2E artifact write failed.", file=sys.stderr)
        raise primary_error
    print("Windows CUA smoke scenario passed.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Native Windows Computer Use Agent Relay E2E")
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--output-file", type=Path)
    args = parser.parse_args(argv)
    try:
        run_scenario(args.evidence_dir, output_file=args.output_file)
    except BaseException:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

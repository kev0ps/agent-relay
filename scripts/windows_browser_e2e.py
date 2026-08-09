#!/usr/bin/env python3
"""Run the bounded native Windows Browser persistent-context smoke scenario."""

from __future__ import annotations

import argparse
import importlib.util
import os
import secrets
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).parents[1].resolve()
FIXTURE_SCRIPT = ROOT / "tests" / "fixtures" / "browser_app.py"


def _load_module(name: str, path: Path) -> Any:
    dotted = f"_agent_relay_windows_browser_{name}"
    cached = sys.modules.get(dotted)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(dotted, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = module
    spec.loader.exec_module(module)
    return module


windows = _load_module("windows_e2e", Path(__file__).with_name("windows_e2e.py"))
try:
    from tests.e2e import mcp_client as portable_mcp
    from tests.e2e import oracles as portable_oracles
    from tests.e2e import scenarios as portable_scenarios
except ModuleNotFoundError as error:
    if error.name not in {"tests", "tests.e2e"}:
        raise
    portable_mcp = _load_module("mcp_client", ROOT / "tests" / "e2e" / "mcp_client.py")
    portable_oracles = _load_module("oracles", ROOT / "tests" / "e2e" / "oracles.py")
    portable_scenarios = _load_module("scenarios", ROOT / "tests" / "e2e" / "scenarios.py")


DEVICE_ID = "windows-browser-e2e-agent"
BROWSER_CAPABILITIES = (
    "browser.back",
    "browser.click",
    "browser.fill",
    "browser.list_tabs",
    "browser.navigate",
    "browser.scroll",
    "browser.snapshot",
    "browser.type",
    "system.ping",
    "terminal.exec",
)
FIXTURE_READY_TIMEOUT_SECONDS = 15.0
AGENT_READY_TIMEOUT_SECONDS = 45.0

WindowsBrowserE2EError = windows.WindowsE2EError


def choose_loopback_port() -> int:
    return windows.choose_loopback_port()


def fixture_command(port: int, run_id: str) -> list[str]:
    """Return the fixed loopback Browser fixture command."""
    windows._validate_port(port)
    if not isinstance(run_id, str) or not run_id or len(run_id) > 64:
        raise ValueError("invalid Browser fixture run id")
    return [
        sys.executable,
        str(FIXTURE_SCRIPT),
        "--run-id",
        run_id,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]


def _playwright_browsers_path() -> str:
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured:
        return configured
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return str(Path(local_app_data) / "ms-playwright")
    return str(Path.home() / "AppData" / "Local" / "ms-playwright")


def _runtime(
    *, mcp_url: str, control_token: str, run_id: str, fixtures_root: Path, fixture_url: str
) -> Any:
    return portable_scenarios.RuntimeConfig(
        mcp_url=mcp_url,
        control_token=control_token,
        device_id=DEVICE_ID,
        run_id=run_id,
        fixture_url=fixture_url,
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
    portable_oracles.validate_status(
        result,
        device_id=None if allow_unenrolled else DEVICE_ID,
        connected=connected,
        expected_capabilities=BROWSER_CAPABILITIES,
        allow_unenrolled=allow_unenrolled,
    )


def _fixture_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}health", timeout=2) as response:
            return response.read() == b'{"status":"ready"}'
    except (OSError, urllib.error.URLError):
        return False


def run_scenario(
    evidence_dir: Path | None = None,
    *,
    output_file: Path | None = None,
) -> None:
    """Run the native Windows Browser persistent-context scenario."""
    if os.name != "nt":
        raise WindowsBrowserE2EError("native Windows Browser harness requires Windows")

    agent_token, control_token = windows.generate_credentials()
    server_port = choose_loopback_port()
    fixture_port = choose_loopback_port()
    run_id = f"windows-browser-{secrets.token_hex(12)}"
    value = f"relay-gh-browser-{run_id}"
    phase = "setup"
    scenario_phase: list[str] = []
    lifecycle = windows.WindowsLifecycle()
    scenario_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    diagnostics: Path | None = None
    server = fixture = agent = None

    try:
        lifecycle.install_signal_handlers()
        temporary = tempfile.TemporaryDirectory(prefix="agent-relay-windows-browser-")
        lifecycle.add_cleanup(temporary.cleanup, label="temporary-directory")
        root = Path(temporary.name)
        home = root / "home"
        workspace = root / "workspace"
        profile = root / "chromium-profile"
        diagnostics = root / "diagnostics"
        local_artifacts = evidence_dir or (root / "browser-evidence")
        home.mkdir()
        workspace.mkdir()
        profile.mkdir()
        diagnostics.mkdir()
        local_artifacts.mkdir(parents=True, exist_ok=True)

        def report_diagnostics() -> None:
            for label in ("server", "fixture", "agent"):
                path = diagnostics / f"{label}.stderr.log"
                if path.exists():
                    print(
                        f"Windows Browser E2E {label} diagnostics: "
                        f"{windows._diagnostic_category(path)}.",
                        file=sys.stderr,
                    )

        lifecycle.add_cleanup(report_diagnostics, label="diagnostic-classification")
        lifecycle.job = windows.WindowsJob()
        lifecycle.add_cleanup(lifecycle.wait_for_diagnostics, label="diagnostics")
        lifecycle.add_cleanup(
            lifecycle.close_diagnostic_streams,
            label="diagnostic-streams",
        )
        lifecycle.add_cleanup(
            lambda: lifecycle.job.terminate(processes=lifecycle.processes),
            label="windows-job",
        )
        repository = ROOT
        fixture_url = f"http://127.0.0.1:{fixture_port}/"
        mcp_url = f"http://127.0.0.1:{server_port}/mcp"

        server_environment = windows.minimal_environment(
            home,
            {
                "RELAY_SERVER_HOST": "127.0.0.1",
                "RELAY_SERVER_PORT": str(server_port),
                "RELAY_MCP_TOKEN": control_token,
                "RELAY_AGENT_TOKEN": agent_token,
                "RELAY_ALLOW_INSECURE_WS": "true",
            },
        )
        agent_environment = windows.minimal_environment(
            home,
            {
                "RELAY_URL": f"ws://127.0.0.1:{server_port}/ws/agent",
                "RELAY_AGENT_TOKEN": agent_token,
                "RELAY_AGENT_ID": DEVICE_ID,
                "RELAY_AGENT_WORKSPACE": str(workspace),
                "RELAY_ALLOW_INSECURE_WS": "true",
                "RELAY_AGENT_HEARTBEAT_INTERVAL_SECONDS": "0.2",
                "RELAY_AGENT_TOOLS": "relay_system_ping,relay_terminal_exec,relay_browser_list_tabs,relay_browser_navigate,relay_browser_snapshot,relay_browser_fill,relay_browser_click,relay_browser_scroll,relay_browser_type,relay_browser_back",
                "RELAY_NATIVE_DEBUG": "1",
                "RELAY_AGENT_BROWSER_USER_DATA_DIR": str(profile),
                "RELAY_AGENT_BROWSER_HEADLESS": "true",
                "RELAY_AGENT_BROWSER_STARTUP_TIMEOUT_SECONDS": "30",
                "PLAYWRIGHT_BROWSERS_PATH": _playwright_browsers_path(),
                "RELAY_AGENT_BROWSER_ALLOWED_ORIGINS": f"http://127.0.0.1:{fixture_port}",
            },
        )
        fixture_environment = windows.minimal_environment(
            home, {"ARTIFACTS_DIR": str(local_artifacts)}
        )
        runtime = _runtime(
            mcp_url=mcp_url,
            control_token=control_token,
            run_id=run_id,
            fixtures_root=local_artifacts,
            fixture_url=fixture_url,
        )

        phase = "server-start"
        server = windows._spawn(
            windows.server_command(server_port),
            environment=server_environment,
            cwd=repository,
            lifecycle=lifecycle,
            diagnostic_file=diagnostics / "server.stderr.log",
        )
        windows._wait_for(
            "Windows Browser server",
            lambda: _status(
                mcp_url, control_token, connected=False, allow_unenrolled=True
            )
            is None,
            timeout=windows.SERVER_READY_TIMEOUT_SECONDS,
        )
        if server.poll() is not None:
            raise WindowsBrowserE2EError("Windows Browser server exited during startup")

        phase = "fixture-start"
        fixture = windows._spawn(
            fixture_command(fixture_port, run_id),
            environment=fixture_environment,
            cwd=repository,
            lifecycle=lifecycle,
            diagnostic_file=diagnostics / "fixture.stderr.log",
        )
        windows._wait_for(
            "Windows Browser fixture",
            lambda: _fixture_ready(fixture_url),
            timeout=FIXTURE_READY_TIMEOUT_SECONDS,
        )
        if fixture.poll() is not None:
            raise WindowsBrowserE2EError("Windows Browser fixture exited during startup")

        phase = "agent-start"
        agent = windows._spawn(
            windows.agent_command(server_port, workspace),
            environment=agent_environment,
            cwd=repository,
            lifecycle=lifecycle,
            diagnostic_file=diagnostics / "agent.stderr.log",
        )

        def agent_ready() -> bool:
            if agent is None or agent.poll() is not None:
                raise WindowsBrowserE2EError("Windows Browser Agent exited during startup")
            _status(mcp_url, control_token, connected=True)
            return True

        windows._wait_for(
            "Windows Browser Agent registration",
            agent_ready,
            timeout=AGENT_READY_TIMEOUT_SECONDS,
        )
        if agent.poll() is not None:
            raise WindowsBrowserE2EError("Windows Browser Agent exited after registration")

        phase = "browser-scenario"
        portable_scenarios.run_browser_scenario(
            runtime,
            value,
            scenario_phase,
            expected_capabilities=BROWSER_CAPABILITIES,
        )
        if any(
            process is not None and process.poll() is not None
            for process in (server, fixture, agent)
        ):
            raise WindowsBrowserE2EError("Windows Browser owned process exited unexpectedly")
    except BaseException as error:
        scenario_error = error

    if not lifecycle._cleaned:
        try:
            lifecycle.cleanup()
        except BaseException as error:
            cleanup_error = error
            lifecycle.cleanup_error = error
            for label in lifecycle.cleanup_failures:
                print(f"Windows Browser E2E cleanup phase: {label}.", file=sys.stderr)

    primary_error = scenario_error or cleanup_error
    if primary_error is None:
        try:
            if output_file is not None:
                windows.write_artifact(
                    output_file.parent,
                    output_file.name,
                    b"Windows Browser smoke scenario passed.\n",
                )
            if evidence_dir is not None:
                windows._write_success(evidence_dir)
        except BaseException as error:
            primary_error = error

    if primary_error is not None:
        detail = (
            f": {primary_error}"
            if isinstance(primary_error, WindowsBrowserE2EError)
            else f": {type(primary_error).__name__}"
        )
        if scenario_phase:
            detail += f" (phase-{scenario_phase[-1]})"
        line = f"Windows Browser E2E failed at scenario-{phase}{detail}."
        print(line, file=sys.stderr)
        if scenario_error is not None and cleanup_error is not None:
            print("Windows Browser E2E cleanup failed.", file=sys.stderr)
        if output_file is not None:
            try:
                windows.write_artifact(
                    output_file.parent, output_file.name, (line + "\n").encode("ascii")
                )
            except BaseException:
                print("Windows Browser E2E artifact write failed.", file=sys.stderr)
        raise primary_error
    print("Windows Browser smoke scenario passed.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Native Windows Browser Agent Relay smoke")
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

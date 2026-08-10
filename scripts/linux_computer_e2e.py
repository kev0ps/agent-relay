#!/usr/bin/env python3
"""Run the bounded Linux Computer Use Agent Relay scenario."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import re
import secrets
import select
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).parents[1].resolve()
DESKTOP_FIXTURE = ROOT / "tests" / "fixtures" / "desktop_app.py"
DISPLAY = ":91"
COMPUTER_APP_NAME = "relay-desktop-fixture"
COMPUTER_WINDOW_TITLE = "Relay Desktop Fixture"


def _load_module(name: str, path: Path) -> Any:
    dotted = f"_agent_relay_linux_computer_{name}"
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


native = _load_module("linux_e2e", Path(__file__).with_name("linux_e2e.py"))
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


DEVICE_ID = "linux-cua-e2e-agent"
CUA_CAPABILITIES = (
    "cua.click",
    "cua.get_window_state",
    "cua.list_windows",
    "cua.type_text",
    "system.ping",
    "terminal.exec",
)
DESKTOP_READY_TIMEOUT_SECONDS = 15.0
FIXTURE_READY_TIMEOUT_SECONDS = 15.0
AGENT_READY_TIMEOUT_SECONDS = 30.0

LinuxCuaE2EError = native.NativeE2EError


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
        portable_oracles.validate_status(
            result,
            device_id=None if allow_unenrolled else DEVICE_ID,
            connected=connected,
            expected_capabilities=CUA_CAPABILITIES,
            allow_unenrolled=allow_unenrolled,
        )
    except ValueError:
        if os.environ.get("RELAY_NATIVE_DEBUG") == "1":
            print(
                "Linux CUA status diagnostic: "
                f"category={portable_oracles.classify_status_failure(result, device_id=None if allow_unenrolled else DEVICE_ID, connected=connected, expected_capabilities=CUA_CAPABILITIES)}",
                file=sys.stderr,
                flush=True,
            )
        raise


def _fixture_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}health", timeout=2) as response:
            return response.read() == b'{"status":"ready"}'
    except (OSError, urllib.error.URLError):
        return False


def _start_dbus(environment: dict[str, str], lifecycle: Any) -> str:
    process = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address=1"],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        shell=False,
        text=True,
    )
    lifecycle.own_process(process)
    if process.stdout is None:
        raise LinuxCuaE2EError("D-Bus stdout is unavailable")
    ready, _, _ = select.select([process.stdout], [], [], DESKTOP_READY_TIMEOUT_SECONDS)
    if not ready:
        raise LinuxCuaE2EError("D-Bus startup timed out")
    address = process.stdout.readline().strip()
    if not address.startswith("unix:"):
        raise LinuxCuaE2EError("D-Bus address is invalid")
    return address


def _run_ready(command: list[str], environment: dict[str, str]) -> bool:
    try:
        subprocess.run(
            command,
            env=environment,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=2,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _x11_ready(environment: dict[str, str]) -> bool:
    return _run_ready(["xdpyinfo", "-display", DISPLAY], environment)


def _accessibility_ready(environment: dict[str, str]) -> bool:
    return _run_ready(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.a11y.Bus",
            "--object-path",
            "/org/a11y/bus",
            "--method",
            "org.a11y.Bus.GetAddress",
        ],
        environment,
    )


def _x11_window_hint(environment: dict[str, str]) -> str:
    """Return bounded X11 window counts without exposing window metadata."""
    counts: dict[str, str] = {}
    probes = {
        "client_windows": ["xprop", "-root", "_NET_CLIENT_LIST_STACKING"],
        "title_windows": [
            "xdotool",
            "search",
            "--onlyvisible",
            "--name",
            COMPUTER_WINDOW_TITLE,
        ],
        "class_windows": [
            "xdotool",
            "search",
            "--onlyvisible",
            "--class",
            COMPUTER_APP_NAME,
        ],
    }
    for label, command in probes.items():
        try:
            completed = subprocess.run(
                command,
                env=environment,
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=2,
                shell=False,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):
            counts[label] = "error"
            continue
        if completed.returncode != 0:
            counts[label] = "unavailable"
            continue
        counts[label] = str(len(re.findall(r"0x[0-9a-fA-F]+", completed.stdout)))
    return " ".join(f"{key}={value}" for key, value in counts.items())


def _x11_has_client_window(environment: dict[str, str]) -> bool:
    """Return whether the X11 window manager has published a client window."""
    try:
        completed = subprocess.run(
            ["xprop", "-root", "_NET_CLIENT_LIST_STACKING"],
            env=environment,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
            shell=False,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and bool(
        re.search(r"0x[0-9a-fA-F]+", completed.stdout)
    )


def _stderr_hint(path: Path) -> str | None:
    """Return a short, redacted diagnostic line without exposing child logs."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    preferred = [
        line
        for line in lines
        if any(
            marker in line.lower()
            for marker in (
                "cua provider descriptor failure:",
                "cua provider inventory failure:",
                "cua catalog construction failed:",
                "computer privacy command failed:",
                "computer startup failed:",
                "computer cua list_windows rejected:",
            )
        )
    ]
    candidates = preferred or [
        line
        for line in lines
        if any(marker in line.lower() for marker in ("error", "fatal", "sandbox", "exception"))
    ]
    for line in reversed(candidates or lines):
        compact = " ".join(line.split())
        if compact:
            compact = re.sub(
                r"(?i)(token|secret|password|authorization)\s*[:=]\s*\S+",
                r"\1=[REDACTED]",
                compact,
            )
            return re.sub(r"[^A-Za-z0-9 _().,:/=+\-]", "", compact)[:240]
    return None


def _event_hint(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    lines = raw.splitlines()
    if not lines:
        return f"bytes={len(raw)} lines=0"
    try:
        payload = json.loads(lines[-1])
    except (UnicodeDecodeError, json.JSONDecodeError):
        return f"bytes={len(raw)} lines={len(lines)} invalid-json"
    if not isinstance(payload, dict):
        return f"bytes={len(raw)} lines={len(lines)} non-object"
    run_id = payload.get("run_id")
    value = payload.get("value")
    return (
        f"bytes={len(raw)} lines={len(lines)} event={payload.get('event')!r} "
        f"run_id_type={type(run_id).__name__} "
        f"run_id_len={len(run_id) if isinstance(run_id, str) else -1} "
        f"value_type={type(value).__name__} "
        f"value_len={len(value) if isinstance(value, str) else -1}"
    )


def _resolve_driver() -> Path:
    try:
        from cua_driver.wrapper import get_binary_path
    except ImportError as error:
        raise LinuxCuaE2EError("cua-driver is unavailable") from error
    path = Path(get_binary_path())
    if not path.is_absolute() or not path.is_file() or path.is_symlink() or not os.access(path, os.X_OK):
        raise LinuxCuaE2EError("cua-driver executable is invalid")
    return path


def chromium_command(executable: Path, profile: Path, fixture_url: str) -> list[str]:
    if not executable.is_absolute() or not profile.is_absolute():
        raise ValueError("Chromium executable and profile must be absolute")
    if not fixture_url.startswith("http://127.0.0.1:") or not fixture_url.endswith("/"):
        raise ValueError("fixture URL must be loopback-only")
    return [
        str(executable),
        f"--app={fixture_url}",
        # GitHub-hosted Ubuntu disables Chromium's unprivileged user namespace
        # sandbox; this process only visits the loopback fixture in an ephemeral job.
        "--no-sandbox",
        "--force-renderer-accessibility",
        "--class=relay-desktop-fixture",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-sync",
        "--window-size=1280,720",
        f"--user-data-dir={profile}",
    ]


def run_scenario(evidence_dir: Path | None = None, *, output_file: Path | None = None) -> None:
    """Run real Server + Agent + public MCP Computer Use calls under Xvfb."""
    if sys.platform != "linux":
        raise LinuxCuaE2EError("Linux Computer Use harness requires Linux")
    if platform.machine() != "x86_64":
        raise LinuxCuaE2EError("Linux Computer Use harness requires x86_64")

    agent_token, control_token = native.generate_credentials()
    server_port = native.choose_loopback_port()
    fixture_port = native.choose_loopback_port()
    run_id = f"linux-cua-{secrets.token_hex(12)}"
    value = f"relay-gh-cua-{run_id}"
    phase = "setup"
    scenario_phase: list[str] = []
    lifecycle = native.NativeLifecycle()
    scenario_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    diagnostics: dict[str, Path] = {}
    event_artifact: Path | None = None
    graphical_environment: dict[str, str] | None = None

    try:
        lifecycle.install_signal_handlers()
        temporary = tempfile.TemporaryDirectory(prefix="agent-relay-linux-cua-")
        lifecycle.add_cleanup(temporary.cleanup)
        root = Path(temporary.name)
        home = root / "home"
        runtime_dir = root / "runtime"
        workspace = root / "workspace"
        profile = root / "chromium-profile"
        local_artifacts = evidence_dir or (root / "computer-evidence")
        event_artifact = local_artifacts / "computer-events.jsonl"
        diagnostics.update(
            {
                "Chromium": root / "chromium.stderr.log",
                "Agent": root / "agent.stderr.log",
            }
        )
        for path in (home, runtime_dir, workspace, profile, local_artifacts):
            path.mkdir(parents=True, exist_ok=True)
        os.chmod(home, 0o700)
        os.chmod(runtime_dir, 0o700)
        repository = ROOT
        desktop_url = f"http://127.0.0.1:{fixture_port}/"
        mcp_url = f"http://127.0.0.1:{server_port}/mcp"
        driver = _resolve_driver()
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            chromium = Path(playwright.chromium.executable_path)
        if not chromium.is_absolute() or not chromium.is_file() or chromium.is_symlink():
            raise LinuxCuaE2EError("Playwright Chromium executable is unavailable")

        graphical_environment = native._minimal_environment(
            home,
            {
                "DISPLAY": DISPLAY,
                "NO_AT_BRIDGE": "0",
                "GTK_MODULES": "gail:atk-bridge",
                "QT_ACCESSIBILITY": "1",
                "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
                "CUA_DRIVER_TELEMETRY": "0",
                "CUA_DRIVER_RS_TELEMETRY_ENABLED": "0",
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_RUNTIME_DIR": str(runtime_dir),
            },
        )
        server_environment = native._minimal_environment(
            home,
            {
                "RELAY_SERVER_HOST": "127.0.0.1",
                "RELAY_SERVER_PORT": str(server_port),
                "RELAY_MCP_TOKEN": control_token,
                "RELAY_AGENT_TOKEN": agent_token,
                "RELAY_ALLOW_INSECURE_WS": "true",
            },
        )
        agent_environment = dict(graphical_environment)
        agent_environment.update(
            {
                "RELAY_URL": f"ws://127.0.0.1:{server_port}/ws/agent",
                "RELAY_AGENT_TOKEN": agent_token,
                "RELAY_AGENT_ID": DEVICE_ID,
                "RELAY_AGENT_WORKSPACE": str(workspace),
                "RELAY_ALLOW_INSECURE_WS": "true",
                "RELAY_AGENT_HEARTBEAT_INTERVAL_SECONDS": "0.2",
                "RELAY_AGENT_TOOLS": "relay_system_ping,relay_terminal_exec,relay_cua_list_windows,relay_cua_get_window_state,relay_cua_click,relay_cua_type_text",
                "RELAY_NATIVE_DEBUG": "1",
                "RELAY_AGENT_COMPUTER_DRIVER_PATH": str(driver),
                "RELAY_AGENT_COMPUTER_ALLOWED_APP_NAME": COMPUTER_APP_NAME,
                "RELAY_AGENT_COMPUTER_ALLOWED_WINDOW_TITLE": COMPUTER_WINDOW_TITLE,
            }
        )
        fixture_environment = native._minimal_environment(home, {"ARTIFACTS_DIR": str(local_artifacts)})
        runtime = _runtime(mcp_url=mcp_url, control_token=control_token, run_id=run_id, fixtures_root=local_artifacts)

        phase = "xvfb-start"
        xvfb = native._spawn(
            ["Xvfb", DISPLAY, "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
            environment=graphical_environment,
            cwd=repository,
            lifecycle=lifecycle,
        )
        native._wait_for("Linux CUA X11", lambda: _x11_ready(graphical_environment), timeout=DESKTOP_READY_TIMEOUT_SECONDS)
        if xvfb.poll() is not None:
            raise LinuxCuaE2EError("Xvfb exited during startup")
        graphical_environment["DBUS_SESSION_BUS_ADDRESS"] = _start_dbus(graphical_environment, lifecycle)
        agent_environment["DBUS_SESSION_BUS_ADDRESS"] = graphical_environment["DBUS_SESSION_BUS_ADDRESS"]
        native._wait_for("Linux CUA accessibility bus", lambda: _accessibility_ready(graphical_environment), timeout=DESKTOP_READY_TIMEOUT_SECONDS)

        phase = "openbox-start"
        openbox = native._spawn(["openbox"], environment=graphical_environment, cwd=repository, lifecycle=lifecycle)
        if openbox.poll() is not None:
            raise LinuxCuaE2EError("Openbox exited during startup")

        phase = "server-start"
        server = native._spawn(native.server_command(server_port), environment=server_environment, cwd=repository, lifecycle=lifecycle)
        native._wait_for(
            "Linux CUA server",
            lambda: _status(
                mcp_url, control_token, connected=False, allow_unenrolled=True
            )
            is None,
            timeout=native.SERVER_READY_TIMEOUT_SECONDS,
        )

        phase = "fixture-start"
        fixture = native._spawn(
            [sys.executable, str(DESKTOP_FIXTURE), "--run-id", run_id, "--port", str(fixture_port)],
            environment=fixture_environment,
            cwd=repository,
            lifecycle=lifecycle,
        )
        native._wait_for("Linux CUA desktop fixture", lambda: _fixture_ready(desktop_url), timeout=FIXTURE_READY_TIMEOUT_SECONDS)

        phase = "chromium-start"
        browser = native._spawn(
            chromium_command(chromium, profile, desktop_url),
            environment=graphical_environment,
            cwd=repository,
            lifecycle=lifecycle,
            stderr_path=diagnostics["Chromium"],
        )
        if browser.poll() is not None:
            raise LinuxCuaE2EError("Linux CUA Chromium exited during startup")

        def chromium_window_ready() -> bool:
            if browser.poll() is not None:
                raise LinuxCuaE2EError("Linux CUA Chromium exited before its window appeared")
            return _x11_has_client_window(graphical_environment)

        native._wait_for(
            "Linux CUA Chromium window",
            chromium_window_ready,
            timeout=DESKTOP_READY_TIMEOUT_SECONDS,
        )

        phase = "agent-start"
        agent = native._spawn(
            native.agent_command(server_port, workspace),
            environment=agent_environment,
            cwd=repository,
            lifecycle=lifecycle,
            stderr_path=diagnostics["Agent"],
        )

        def agent_ready() -> bool:
            if agent.poll() is not None:
                raise LinuxCuaE2EError("Linux CUA Agent exited during startup")
            _status(mcp_url, control_token, connected=True)
            return True

        native._wait_for("Linux CUA Agent registration", agent_ready, timeout=AGENT_READY_TIMEOUT_SECONDS)
        phase = "computer-scenario"
        portable_scenarios.run_cua_scenario(
            runtime,
            value,
            scenario_phase,
            expected_capabilities=CUA_CAPABILITIES,
            expected_cua_app=COMPUTER_APP_NAME,
            expected_cua_window_title=COMPUTER_WINDOW_TITLE,
        )
        if any(process.poll() is not None for process in (server, fixture, browser, agent)):
            raise LinuxCuaE2EError("Linux CUA owned process exited unexpectedly")
    except BaseException as error:
        scenario_error = error
        if graphical_environment is not None:
            print(
                f"Linux CUA X11 diagnostic: {_x11_window_hint(graphical_environment)}",
                file=sys.stderr,
            )
        for label, path in diagnostics.items():
            if (hint := _stderr_hint(path)) is not None:
                print(f"Linux CUA {label} diagnostic: {hint}", file=sys.stderr)
        if event_artifact is not None:
            print(
                f"Linux CUA event diagnostic: {_event_hint(event_artifact)}",
                file=sys.stderr,
            )

    if not lifecycle._cleaned:
        try:
            lifecycle.cleanup()
        except BaseException as error:
            cleanup_error = error

    primary_error = scenario_error or cleanup_error
    if primary_error is None:
        try:
            if output_file is not None:
                native._write_artifact(output_file.parent, output_file.name, b"Linux CUA smoke scenario passed.\n")
            if evidence_dir is not None:
                native._write_success(evidence_dir)
        except BaseException as error:
            primary_error = error

    if primary_error is not None:
        detail = f": {primary_error}" if isinstance(primary_error, LinuxCuaE2EError) else f": {type(primary_error).__name__}"
        if scenario_phase:
            detail += f" (phase-{scenario_phase[-1]})"
        line = f"Linux CUA E2E failed at scenario-{phase}{detail}."
        print(line, file=sys.stderr)
        if scenario_error is not None and cleanup_error is not None:
            print("Linux CUA E2E cleanup failed.", file=sys.stderr)
        if output_file is not None:
            try:
                native._write_artifact(output_file.parent, output_file.name, (line + "\n").encode("ascii"))
            except BaseException:
                print("Linux CUA E2E artifact write failed.", file=sys.stderr)
        raise primary_error
    print("Linux CUA smoke scenario passed.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Linux Computer Use Agent Relay smoke")
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

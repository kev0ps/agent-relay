#!/usr/bin/env python3
"""Linux Computer Use adapter for the shared native E2E harness."""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import re
import select
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
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


native = _load_module(
    "_agent_relay_linux_e2e_adapter",
    Path(__file__).with_name("linux_e2e_adapter.py"),
)
harness = native.harness
portable_mcp = harness.portable_mcp
portable_oracles = harness.portable_oracles
portable_scenarios = harness.portable_scenarios

ROOT = Path(__file__).parents[1].resolve()
DESKTOP_FIXTURE = ROOT / "tests" / "fixtures" / "desktop_app.py"
DISPLAY = ":91"
COMPUTER_APP_NAME = "relay-desktop-fixture"
COMPUTER_WINDOW_TITLE = "Relay Desktop Fixture"
SNAP_CHROMIUM_BIN_DIR = Path("/snap/bin")


DEVICE_ID = "linux-cua-e2e-agent"
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
DESKTOP_READY_TIMEOUT_SECONDS = 15.0
FIXTURE_READY_TIMEOUT_SECONDS = 15.0
AGENT_READY_TIMEOUT_SECONDS = 30.0
CUA_EXISTING_PROFILE_GRANT_ENV = "AGENT_RELAY_CUA_GRANT_EXISTING_PROFILE"

LinuxCuaE2EError = native.NativeE2EError


def _runtime(
    *,
    mcp_url: str,
    control_token: str,
    run_id: str,
    fixtures_root: Path,
    fixture_url: str,
    browser_pid: str = "",
    browser_launch_path: str = "",
) -> Any:
    return portable_scenarios.RuntimeConfig(
        mcp_url=mcp_url,
        control_token=control_token,
        device_id=DEVICE_ID,
        run_id=run_id,
        fixture_url=fixture_url,
        fixtures_root=str(fixtures_root),
        browser_pid=browser_pid,
        browser_launch_path=browser_launch_path,
    )


def _cua_agent_driver_environment() -> dict[str, str]:
    """Pass the explicit existing-profile opt-in only to the Agent child."""
    if os.environ.get(CUA_EXISTING_PROFILE_GRANT_ENV) == "1":
        return {CUA_EXISTING_PROFILE_GRANT_ENV: "1"}
    return {}


_CUA_FIELD_ROLES = frozenset({"textbox", "entry", "text", "edit", "editable"})
_CUA_BUTTON_ROLES = frozenset({"button", "push button"})


def _cua_snapshot_diagnostic(result: Any) -> str:
    payload = getattr(result, "structured_content", getattr(result, "structuredContent", None))
    if not isinstance(payload, dict):
        return "structured_content=unavailable"
    elements = payload.get("elements")
    if not isinstance(elements, list):
        return f"elements_type={type(elements).__name__}"
    field_roles = 0
    button_roles = 0
    labeled_elements = 0
    nonempty_labels = 0
    name_labels = 0
    apply_labels = 0
    for element in elements:
        if not isinstance(element, dict):
            continue
        role = element.get("role")
        if isinstance(role, str):
            folded_role = role.casefold()
            if folded_role in _CUA_FIELD_ROLES:
                field_roles += 1
            elif folded_role in _CUA_BUTTON_ROLES:
                button_roles += 1
        label = element.get("label")
        if not isinstance(label, str):
            continue
        labeled_elements += 1
        if label:
            nonempty_labels += 1
        if label.casefold() == "name":
            name_labels += 1
        elif label.casefold() == "apply":
            apply_labels += 1
    return (
        f"element_count={len(elements)} field_roles={field_roles} "
        f"button_roles={button_roles} labeled_elements={labeled_elements} "
        f"nonempty_labels={nonempty_labels} name_labels={name_labels} "
        f"apply_labels={apply_labels} degraded={payload.get('degraded') is True} "
        f"has_snapshot_id={isinstance(payload.get('snapshot_id'), str)}"
    )


def _cua_controls_ready(runtime: Any) -> bool:
    """Require the public CUA path to expose the fixture's two controls."""
    snapshot: Any | None = None
    try:
        raw_browser_pid = getattr(runtime, "browser_pid", "")
        browser_pid: int | None = None
        list_arguments: dict[str, int] = {}
        if raw_browser_pid:
            if (
                not isinstance(raw_browser_pid, str)
                or not raw_browser_pid.isascii()
                or not raw_browser_pid.isdecimal()
            ):
                return False
            browser_pid = int(raw_browser_pid)
            if browser_pid <= 0:
                return False
            list_arguments["pid"] = browser_pid
        listed = portable_mcp.call_tool(
            runtime.mcp_url,
            runtime.control_token,
            "relay_cua_list_windows",
            list_arguments,
            http_timeout=1.0,
            operation_timeout=2.0,
        )
        if browser_pid is None:
            pid, window_id = portable_oracles.validate_cua_list_windows(
                listed,
                expected_app=COMPUTER_APP_NAME,
                expected_window_title=COMPUTER_WINDOW_TITLE,
            )
        else:
            pid, window_id = portable_oracles.validate_cua_list_windows(
                listed,
                expected_pid=browser_pid,
            )
        snapshot = portable_mcp.call_tool(
            runtime.mcp_url,
            runtime.control_token,
            "relay_cua_get_window_state",
            {
                "pid": pid,
                "window_id": window_id,
                "include_screenshot": False,
                "max_elements": 128,
            },
            http_timeout=1.0,
            operation_timeout=2.0,
        )
        portable_oracles.validate_cua_window_state(
            snapshot,
            expected_pid=pid,
            window_id=window_id,
        )
    except (ConnectionError, ValueError):
        if snapshot is not None:
            print(
                f"Linux CUA AX diagnostic: {_cua_snapshot_diagnostic(snapshot)}",
                file=sys.stderr,
                flush=True,
            )
        return False
    return True


def _launch_cua_browser(runtime: Any, profile: Path, executable: Path) -> int:
    """Launch the fixture through the public CUA path exactly once."""
    if not executable.is_absolute() or not executable.is_file():
        raise LinuxCuaE2EError("a compatible Chromium executable is unavailable")
    result = portable_mcp.call_tool(
        runtime.mcp_url,
        runtime.control_token,
        "relay_cua_launch_app",
        {
            "name": executable.name,
            "launch_path": str(executable),
            "additional_arguments": [
                f"--user-data-dir={profile}",
                f"--app={runtime.fixture_url}",
                "--class=relay-desktop-fixture",
                "--window-name=Relay Desktop Fixture",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-sync",
                "--window-size=1280,720",
                "--force-renderer-accessibility",
            ],
        },
        http_timeout=10.0,
        operation_timeout=15.0,
    )
    return portable_oracles.validate_cua_browser_launch(result)


def _kill_cua_browser(runtime: Any, pid: int) -> None:
    """Release a CUA-launched browser on harness failure."""
    try:
        result = portable_mcp.call_tool(
            runtime.mcp_url,
            runtime.control_token,
            "relay_cua_kill_app",
            {"pid": pid},
            http_timeout=2.0,
            operation_timeout=10.0,
        )
    except BaseException as error:
        print(
            f"Linux CUA desktop kill diagnostic: pid={pid} call_error={type(error).__name__}",
            file=sys.stderr,
        )
        raise
    try:
        portable_oracles.validate_cua_browser_success(
            result,
            tool_name="relay_cua_kill_app",
        )
    except BaseException:
        payload = getattr(result, "structured_content", getattr(result, "structuredContent", None))
        keys = ",".join(sorted(str(key) for key in payload)) if isinstance(payload, dict) else "none"
        print(
            "Linux CUA desktop kill diagnostic: "
            f"pid={pid} is_error={getattr(result, 'is_error', getattr(result, 'isError', None)) is True} "
            f"structured_keys={keys} content_count={len(getattr(result, 'content', []) or [])}.",
            file=sys.stderr,
        )
        raise


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


def _enable_chromium_accessibility(environment: dict[str, str]) -> bool:
    """Advertise an assistive technology on the isolated session bus."""
    try:
        for property_name in ("ScreenReaderEnabled", "IsEnabled"):
            subprocess.run(
                [
                    "gdbus",
                    "call",
                    "--session",
                    "--dest",
                    "org.a11y.Bus",
                    "--object-path",
                    "/org/a11y/bus",
                    "--method",
                    "org.freedesktop.DBus.Properties.Set",
                    "org.a11y.Status",
                    property_name,
                    "<true>",
                ],
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


def _read_at_spi_bus_address(environment: dict[str, str]) -> str:
    """Resolve the isolated AT-SPI bus address for the driver and browser."""
    try:
        completed = subprocess.run(
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
    except (OSError, subprocess.SubprocessError) as error:
        raise LinuxCuaE2EError("AT-SPI bus address is unavailable") from error
    match = re.fullmatch(r"\('(?P<address>unix:[^']+)'\s*,?\)\s*", completed.stdout)
    if completed.returncode != 0 or match is None:
        raise LinuxCuaE2EError("AT-SPI bus address is invalid")
    return match.group("address")


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


def _x11_search_ids(
    environment: dict[str, str], option: str, value: str
) -> set[str]:
    try:
        completed = subprocess.run(
            ["xdotool", "search", "--onlyvisible", option, value],
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
        return set()
    if completed.returncode != 0:
        return set()
    return {line.strip() for line in completed.stdout.splitlines() if line.strip()}


def _x11_has_expected_window(environment: dict[str, str]) -> bool:
    """Wait for the browser's final X11 class and HTML title, not just its frame."""
    titles = _x11_search_ids(
        environment,
        "--name",
        f"^{re.escape(COMPUTER_WINDOW_TITLE)}$",
    )
    classes = _x11_search_ids(
        environment,
        "--class",
        f"^{re.escape(COMPUTER_APP_NAME)}$",
    )
    return bool(titles & classes)


def _resolve_chromium() -> Path:
    """Resolve the browser supplied by the Linux E2E environment."""
    for name in (
        "google-chrome-stable",
        "google-chrome",
        "chromium",
        "chromium-browser",
    ):
        raw_path = shutil.which(name)
        if raw_path is None:
            continue
        candidate = Path(raw_path)
        if candidate.is_absolute() and candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise LinuxCuaE2EError("a compatible Chromium executable is unavailable")


def chromium_command(executable: Path, profile: Path, fixture_url: str) -> list[str]:
    """Build the isolated browser command used by the integrated CUA path."""
    if not executable.is_absolute() or not profile.is_absolute():
        raise ValueError("Chromium executable and profile must be absolute")
    if not fixture_url.startswith("http://127.0.0.1:") or not fixture_url.endswith("/"):
        raise ValueError("fixture URL must be loopback-only")
    return [
        str(executable),
        f"--app={fixture_url}",
        "--no-sandbox",
        "--force-renderer-accessibility",
        "--class=relay-desktop-fixture",
        "--window-name=Relay Desktop Fixture",
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


def chromium_environment(
    executable: Path,
    environment: dict[str, str],
    *,
    host_runtime_dir: Path | None,
    host_session_bus_address: str | None,
) -> dict[str, str]:
    """Give a snap Chromium the host user bus needed for its systemd scope."""
    result = dict(environment)
    if not _is_snap_chromium_launcher(executable):
        return result
    if host_session_bus_address:
        result["DBUS_SESSION_BUS_ADDRESS"] = host_session_bus_address
        return result
    if host_runtime_dir is None:
        return result
    bus = host_runtime_dir / "bus"
    if bus.is_socket():
        result["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    return result


def _is_snap_chromium_launcher(executable: Path) -> bool:
    """Recognize snap Chromium without replacing its launcher symlink."""
    current = executable
    visited: set[Path] = set()
    for _ in range(8):
        if current.parent == SNAP_CHROMIUM_BIN_DIR:
            return True
        if current in visited or not current.is_symlink():
            return False
        visited.add(current)
        try:
            target = current.readlink()
        except OSError:
            return False
        current = target if target.is_absolute() else current.parent / target
    return False


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
                "agent invocation failed:",
                "computer cua driver exited:",
                "computer cua driver response failed:",
                "computer cua get_window_state rejected:",
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



def validate_host() -> None:
    if sys.platform != "linux":
        raise LinuxCuaE2EError("Linux Computer Use harness requires Linux")
    if platform.machine() != "x86_64":
        raise LinuxCuaE2EError("Linux Computer Use harness requires x86_64")


def create_context(
    root: Path,
    evidence_dir: Path | None,
    agent_token: str,
    control_token: str,
    run_id: str,
    value: str,
    lifecycle: Any,
) -> Any:
    server_port = native.choose_loopback_port()
    fixture_port = native.choose_loopback_port()
    home = root / "home"
    runtime_dir = root / "runtime"
    workspace = root / "workspace"
    profile = root / "chromium-profile"
    local_artifacts = evidence_dir or (root / "computer-evidence")
    event_artifact = local_artifacts / "computer-events.jsonl"
    diagnostics = {
        "Chromium": root / "chromium.stderr.log",
        "Agent": root / "agent.stderr.log",
    }
    for path in (home, runtime_dir, workspace, profile, local_artifacts):
        path.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    os.chmod(runtime_dir, 0o700)
    repository = ROOT
    desktop_url = f"http://127.0.0.1:{fixture_port}/"
    mcp_url = f"http://127.0.0.1:{server_port}/mcp"
    graphical_values = {
        "DISPLAY": DISPLAY,
        "ACCESSIBILITY_ENABLED": "1",
        "NO_AT_BRIDGE": "0",
        "GTK_MODULES": "gail:atk-bridge",
        "QT_ACCESSIBILITY": "1",
        "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
        "CUA_DRIVER_TELEMETRY": "0",
        "CUA_DRIVER_RS_TELEMETRY_ENABLED": "0",
        "CUA_E2E_BROWSER_NO_SANDBOX": "1",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_RUNTIME_DIR": str(runtime_dir),
    }
    graphical_environment = native._minimal_environment(home, graphical_values)
    server_environment = native._minimal_environment(
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
            "RELAY_AGENT_ID": DEVICE_ID,
            "RELAY_AGENT_WORKSPACE": str(workspace),
            "RELAY_AGENT_HEARTBEAT_INTERVAL_SECONDS": "0.2",
            "RELAY_AGENT_TOOLS": ",".join(CUA_AGENT_TOOLS),
            "RELAY_NATIVE_DEBUG": "1",
            "RELAY_AGENT_COMPUTER_ALLOWED_APP_NAME": COMPUTER_APP_NAME,
            "RELAY_AGENT_COMPUTER_ALLOWED_WINDOW_TITLE": COMPUTER_WINDOW_TITLE,
            "RELAY_AGENT_COMPUTER_ACTION_TIMEOUT_SECONDS": "30",
        }
    )
    agent_environment.update(_cua_agent_driver_environment())
    fixture_environment = native._minimal_environment(
        home, {"ARTIFACTS_DIR": str(local_artifacts)}
    )
    runtime = _runtime(
        mcp_url=mcp_url,
        control_token=control_token,
        run_id=run_id,
        fixtures_root=local_artifacts,
        fixture_url=desktop_url,
    )
    context = harness.CuaContext(
        lifecycle=lifecycle,
        root=root,
        home=home,
        workspace=workspace,
        artifacts=local_artifacts,
        repository=repository,
        mcp_url=mcp_url,
        runtime=runtime,
        value=value,
        run_id=run_id,
        diagnostics=diagnostics,
    )
    context.metadata.update(
        {
            "server_port": server_port,
            "fixture_port": fixture_port,
            "desktop_url": desktop_url,
            "profile": profile,
            "graphical_environment": graphical_environment,
            "server_environment": server_environment,
            "agent_environment": agent_environment,
            "fixture_environment": fixture_environment,
            "event_artifact": event_artifact,
            "browser_pid": None,
        }
    )
    return context


def prepare_platform(context: Any) -> None:
    environment = context.metadata["graphical_environment"]
    context.phase = "xvfb-start"
    xvfb = native._spawn(
        ["Xvfb", DISPLAY, "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
        environment=environment,
        cwd=context.repository,
        lifecycle=context.lifecycle,
    )
    native._wait_for(
        "Linux CUA X11",
        lambda: _x11_ready(environment),
        timeout=DESKTOP_READY_TIMEOUT_SECONDS,
    )
    if xvfb.poll() is not None:
        raise LinuxCuaE2EError("Xvfb exited during startup")
    environment["DBUS_SESSION_BUS_ADDRESS"] = _start_dbus(
        environment, context.lifecycle
    )
    agent_environment = context.metadata["agent_environment"]
    agent_environment["DBUS_SESSION_BUS_ADDRESS"] = environment[
        "DBUS_SESSION_BUS_ADDRESS"
    ]
    native._wait_for(
        "Linux CUA accessibility bus",
        lambda: _accessibility_ready(environment),
        timeout=DESKTOP_READY_TIMEOUT_SECONDS,
    )
    native._wait_for(
        "Linux CUA Chromium accessibility",
        lambda: _enable_chromium_accessibility(environment),
        timeout=DESKTOP_READY_TIMEOUT_SECONDS,
    )
    at_spi_bus_address = _read_at_spi_bus_address(environment)
    environment["AT_SPI_BUS_ADDRESS"] = at_spi_bus_address
    agent_environment["AT_SPI_BUS_ADDRESS"] = at_spi_bus_address
    context.phase = "openbox-start"
    openbox = native._spawn(
        ["openbox"],
        environment=environment,
        cwd=context.repository,
        lifecycle=context.lifecycle,
    )
    if openbox.poll() is not None:
        raise LinuxCuaE2EError("Openbox exited during startup")


def start_server(context: Any) -> Any:
    return native._spawn(
        native.server_command(context.metadata["server_port"]),
        environment=context.metadata["server_environment"],
        cwd=context.repository,
        lifecycle=context.lifecycle,
    )


def wait_server(context: Any) -> None:
    native._wait_for(
        "Linux CUA server",
        lambda: _status(
            context.mcp_url,
            context.runtime.control_token,
            connected=False,
            allow_unenrolled=True,
        )
        is None,
        timeout=native.SERVER_READY_TIMEOUT_SECONDS,
    )


def start_fixture(context: Any) -> Any:
    return native._spawn(
        [
            sys.executable,
            str(DESKTOP_FIXTURE),
            "--run-id",
            context.run_id,
            "--port",
            str(context.metadata["fixture_port"]),
        ],
        environment=context.metadata["fixture_environment"],
        cwd=context.repository,
        lifecycle=context.lifecycle,
    )


def wait_fixture(context: Any) -> None:
    native._wait_for(
        "Linux CUA desktop fixture",
        lambda: _fixture_ready(context.metadata["desktop_url"]),
        timeout=FIXTURE_READY_TIMEOUT_SECONDS,
    )


def start_agent(context: Any) -> Any:
    return native._spawn(
        native.agent_command(context.metadata["server_port"], context.workspace),
        environment=context.metadata["agent_environment"],
        cwd=context.repository,
        lifecycle=context.lifecycle,
        stderr_path=context.diagnostics["Agent"],
    )


def wait_agent(context: Any) -> None:
    def ready() -> bool:
        if context.agent is not None and context.agent.poll() is not None:
            raise LinuxCuaE2EError("Linux CUA Agent exited during startup")
        _status(context.mcp_url, context.runtime.control_token, connected=True)
        return True

    native._wait_for(
        "Linux CUA Agent registration",
        ready,
        timeout=AGENT_READY_TIMEOUT_SECONDS,
    )


def prepare_scenario(context: Any) -> None:
    context.phase = "chromium-start"
    chromium = _resolve_chromium()
    profile = context.metadata["profile"]
    browser_pid = _launch_cua_browser(context.runtime, profile, chromium)
    context.metadata["browser_pid"] = browser_pid
    context.runtime = _runtime(
        mcp_url=context.mcp_url,
        control_token=context.runtime.control_token,
        run_id=context.run_id,
        fixtures_root=context.artifacts,
        fixture_url=context.runtime.fixture_url,
        browser_launch_path=str(chromium),
    )
    context.lifecycle.add_cleanup(
        lambda: _kill_cua_browser(context.runtime, context.metadata["browser_pid"])
        if context.metadata["browser_pid"] is not None
        else None
    )
    native._wait_for(
        "Linux CUA browser accessibility controls",
        lambda: _cua_controls_ready(context.runtime),
        timeout=DESKTOP_READY_TIMEOUT_SECONDS,
    )
    context.phase = "cua-scenario"


def run_scenario(context: Any, phase: list[str]) -> None:
    context.metadata["scenario_phase"] = phase
    portable_scenarios.run_cua_scenario(
        context.runtime,
        context.value,
        phase,
        expected_capabilities=CUA_CAPABILITIES,
        expected_cua_app=COMPUTER_APP_NAME,
        expected_cua_window_title=COMPUTER_WINDOW_TITLE,
        include_browser=True,
    )
    browser_pid = context.metadata["browser_pid"]
    if browser_pid is not None and context.runtime.browser_pid == str(browser_pid):
        context.metadata["browser_pid"] = None


def assert_processes(context: Any) -> None:
    if any(
        process.poll() is not None
        for process in (context.server, context.fixture, context.agent)
        if process is not None
    ):
        raise LinuxCuaE2EError("Linux CUA owned process exited unexpectedly")


def report_failure(context: Any, _error: BaseException) -> None:
    phase = context.metadata.get("scenario_phase", [])
    browser_pid = context.metadata.get("browser_pid")
    if any(item.startswith("browser-") for item in phase) and (
        browser_pid is not None
        and context.runtime.browser_pid == str(browser_pid)
    ):
        context.metadata["browser_pid"] = None
    environment = context.metadata.get("graphical_environment")
    if environment is not None:
        print(
            f"Linux CUA X11 diagnostic: {_x11_window_hint(environment)}",
            file=sys.stderr,
        )
    for label, path in context.diagnostics.items():
        if (hint := _stderr_hint(path)) is not None:
            print(f"Linux CUA {label} diagnostic: {hint}", file=sys.stderr)
    event_artifact = context.metadata.get("event_artifact")
    if event_artifact is not None:
        print(
            f"Linux CUA event diagnostic: {_event_hint(event_artifact)}",
            file=sys.stderr,
        )


def report_after_cleanup(
    _context: Any,
    _scenario_error: BaseException | None,
    _cleanup_error: BaseException | None,
) -> None:
    return None


def write_artifact(evidence_dir: Path, name: str, payload: bytes) -> None:
    native._write_artifact(evidence_dir, name, payload)


def make_adapter() -> Any:
    return harness.CuaAdapter(
        label="Linux CUA",
        run_id_prefix="linux-cua-",
        temp_prefix="agent-relay-linux-cua-",
        success_message="Linux CUA smoke scenario passed.",
        failure_prefix="Linux CUA E2E failed at scenario-",
        cleanup_message="Linux CUA E2E cleanup failed",
        error_type=LinuxCuaE2EError,
        lifecycle_factory=lambda: native.NativeLifecycle(),
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
    )

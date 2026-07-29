#!/usr/bin/env python3
"""Run the bounded native Windows Browser/CDP Agent Relay smoke scenario."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import importlib.util
import json
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
    "browser.click",
    "browser.fill",
    "browser.list_tabs",
    "browser.navigate",
    "browser.read_page",
    "system.ping",
    "terminal.exec",
)
POLL_INTERVAL_SECONDS = 0.1
SERVER_READY_TIMEOUT_SECONDS = 15.0
FIXTURE_READY_TIMEOUT_SECONDS = 15.0
CHROMIUM_READY_TIMEOUT_SECONDS = 30.0
AGENT_READY_TIMEOUT_SECONDS = 30.0
CDP_SCREENSHOT_TIMEOUT_SECONDS = 15.0
MAX_CDP_FRAME_BYTES = 1024 * 1024
MAX_SCREENSHOT_BYTES = 512 * 1024
MAX_SCREENSHOT_DIMENSION = 4096


WindowsBrowserE2EError = windows.WindowsE2EError


def choose_loopback_port() -> int:
    return windows.choose_loopback_port()


def chromium_command(
    executable: Path, cdp_port: int, profile: Path
) -> list[str]:
    """Return the fixed headless Chromium command used by the smoke."""
    windows._validate_port(cdp_port)
    if not isinstance(executable, Path) or not isinstance(profile, Path):
        raise ValueError("Chromium executable and profile must be Paths")
    if not profile.is_absolute():
        raise ValueError("Chromium profile must be absolute")
    return [
        str(executable),
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-sync",
        "--disable-extensions",
        "--window-size=1280,800",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={profile}",
        "about:blank",
    ]


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


def resolve_chromium_executable(explicit: Path | None = None) -> Path:
    """Resolve a Playwright-installed or explicitly pinned Chromium executable."""
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    environment_path = os.environ.get("AGENT_RELAY_CHROMIUM_PATH")
    if environment_path:
        candidates.append(Path(environment_path))

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            candidates.append(Path(playwright.chromium.executable_path))
    except (ImportError, OSError, RuntimeError):
        pass

    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
        root = os.environ.get(variable)
        if root:
            candidates.append(Path(root) / "Google/Chrome/Application/chrome.exe")
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(
            Path(local_app_data) / "Google/Chrome/Application/chrome.exe"
        )

    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    raise WindowsBrowserE2EError("Chromium executable is unavailable")


def _url_json(url: str) -> object:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=2) as response:
        return json.loads(response.read(MAX_CDP_FRAME_BYTES + 1))


def _cdp_ready(cdp_url: str) -> bool:
    try:
        payload = _url_json(f"{cdp_url}/json/version")
    except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("Browser"), str)
        and isinstance(payload.get("webSocketDebuggerUrl"), str)
    )


def _fixture_page_socket(cdp_url: str, fixture_url: str) -> str:
    try:
        payload = _url_json(f"{cdp_url}/json/list")
    except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as error:
        raise WindowsBrowserE2EError("Chromium page listing failed") from error
    if not isinstance(payload, list) or not 1 <= len(payload) <= 8:
        raise WindowsBrowserE2EError("Chromium page listing is invalid")
    matches = [
        item
        for item in payload
        if isinstance(item, dict)
        and item.get("type") == "page"
        and item.get("url") == fixture_url
        and isinstance(item.get("webSocketDebuggerUrl"), str)
    ]
    if len(matches) != 1:
        raise WindowsBrowserE2EError("Chromium fixture page identity is invalid")
    return matches[0]["webSocketDebuggerUrl"]


async def _capture_png(ws_url: str) -> bytes:
    try:
        import websockets
    except ImportError as error:
        raise WindowsBrowserE2EError("WebSocket client is unavailable") from error

    async with websockets.connect(
        ws_url,
        open_timeout=2,
        close_timeout=2,
        max_size=MAX_CDP_FRAME_BYTES,
    ) as socket:
        await socket.send(
            json.dumps(
                {
                    "id": 1,
                    "method": "Page.captureScreenshot",
                    "params": {"format": "png"},
                },
                separators=(",", ":"),
            )
        )
        while True:
            raw = await asyncio.wait_for(socket.recv(), timeout=5)
            if not isinstance(raw, str):
                raise WindowsBrowserE2EError("Chromium CDP returned binary framing")
            message = json.loads(raw)
            if not isinstance(message, dict) or message.get("id") != 1:
                continue
            if set(message) - {"id", "result", "error"}:
                raise WindowsBrowserE2EError("Chromium CDP returned extra fields")
            if "error" in message:
                raise WindowsBrowserE2EError("Chromium screenshot request failed")
            result = message.get("result")
            if not isinstance(result, dict) or set(result) != {"data"}:
                raise WindowsBrowserE2EError("Chromium screenshot result is invalid")
            data = result["data"]
            if not isinstance(data, str) or len(data) > MAX_SCREENSHOT_BYTES * 2:
                raise WindowsBrowserE2EError("Chromium screenshot is oversized")
            try:
                image = base64.b64decode(data, validate=True)
            except (binascii.Error, ValueError) as error:
                raise WindowsBrowserE2EError("Chromium screenshot encoding is invalid") from error
            validate_screenshot_png(image)
            return image


def validate_screenshot_png(payload: bytes) -> tuple[int, int]:
    """Validate a bounded PNG signature, IHDR, and non-zero dimensions."""
    if not isinstance(payload, bytes) or not 24 <= len(payload) <= MAX_SCREENSHOT_BYTES:
        raise WindowsBrowserE2EError("Chromium screenshot is not a bounded PNG")
    if payload[:8] != b"\x89PNG\r\n\x1a\n" or payload[12:16] != b"IHDR":
        raise WindowsBrowserE2EError("Chromium screenshot is not a bounded PNG")
    width = int.from_bytes(payload[16:20], "big")
    height = int.from_bytes(payload[20:24], "big")
    if not 1 <= width <= MAX_SCREENSHOT_DIMENSION or not 1 <= height <= MAX_SCREENSHOT_DIMENSION:
        raise WindowsBrowserE2EError("Chromium screenshot dimensions are invalid")
    return width, height


def write_screenshot(evidence_dir: Path, payload: bytes) -> None:
    """Write one bounded PNG without following a reparse point."""
    validate_screenshot_png(payload)
    windows._reject_reparse_ancestors(evidence_dir)
    evidence_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    windows._reject_reparse_ancestors(evidence_dir)
    target = evidence_dir / "screenshot.png"
    windows._reject_reparse_ancestors(target)
    try:
        with target.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise WindowsBrowserE2EError("screenshot artifact already exists") from None


def capture_screenshot(cdp_url: str, fixture_url: str, evidence_dir: Path | None) -> None:
    """Capture the exact fixture target through raw CDP and validate its PNG."""
    ws_url = _fixture_page_socket(cdp_url, fixture_url)
    try:
        image = asyncio.run(
            asyncio.wait_for(
                _capture_png(ws_url),
                timeout=CDP_SCREENSHOT_TIMEOUT_SECONDS,
            )
        )
    except TimeoutError:
        raise WindowsBrowserE2EError("Chromium screenshot timed out") from None
    if evidence_dir is not None:
        write_screenshot(evidence_dir, image)


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


def _status(mcp_url: str, control_token: str, *, connected: bool) -> None:
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
        device_id=DEVICE_ID,
        connected=connected,
        expected_capabilities=BROWSER_CAPABILITIES,
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
    """Run the native Windows Browser/CDP scenario with bounded cleanup."""
    if os.name != "nt":
        raise WindowsBrowserE2EError("native Windows Browser harness requires Windows")

    agent_token, control_token = windows.generate_credentials()
    server_port = choose_loopback_port()
    fixture_port = choose_loopback_port()
    cdp_port = choose_loopback_port()
    run_id = f"windows-browser-{secrets.token_hex(12)}"
    value = f"relay-gh-browser-{run_id}"
    phase = "setup"
    scenario_phase: list[str] = []
    lifecycle = windows.WindowsLifecycle()
    scenario_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    diagnostics: Path | None = None

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
            for label in ("server", "fixture", "chromium", "agent"):
                path = diagnostics / f"{label}.stderr.log"
                if path.exists():
                    print(
                        f"Windows Browser E2E {label} diagnostics: {windows._diagnostic_category(path)}.",
                        file=sys.stderr,
                    )

        lifecycle.add_cleanup(
            report_diagnostics,
            label="diagnostic-classification",
        )
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
        chromium = resolve_chromium_executable()

        server_environment = windows.minimal_environment(
            home,
            {
                "AGENT_RELAY_DEVICE_ID": DEVICE_ID,
                "AGENT_RELAY_AGENT_TOKEN": agent_token,
                "AGENT_RELAY_CONTROL_TOKEN": control_token,
                "AGENT_RELAY_PORT": str(server_port),
            },
        )
        agent_environment = windows.minimal_environment(
            home,
            {
                "AGENT_RELAY_DEVICE_ID": DEVICE_ID,
                "AGENT_RELAY_AGENT_TOKEN": agent_token,
                "AGENT_RELAY_SERVER_URL": f"ws://127.0.0.1:{server_port}/ws/agent",
                "AGENT_RELAY_WORKSPACE": str(workspace),
                "AGENT_RELAY_HEARTBEAT_INTERVAL_SECONDS": "0.2",
                "AGENT_RELAY_BROWSER_CDP_URL": f"http://127.0.0.1:{cdp_port}",
                "AGENT_RELAY_BROWSER_ALLOWED_ORIGINS": f"http://127.0.0.1:{fixture_port}",
            },
        )
        fixture_environment = windows.minimal_environment(
            home,
            {"ARTIFACTS_DIR": str(local_artifacts)},
        )
        chromium_environment = windows.minimal_environment(home, {})
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
            lambda: _status(mcp_url, control_token, connected=False) is None,
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

        phase = "chromium-start"
        browser = windows._spawn(
            chromium_command(chromium, cdp_port, profile),
            environment=chromium_environment,
            cwd=repository,
            lifecycle=lifecycle,
            diagnostic_file=diagnostics / "chromium.stderr.log",
        )
        windows._wait_for(
            "Windows Chromium CDP",
            lambda: _cdp_ready(f"http://127.0.0.1:{cdp_port}"),
            timeout=CHROMIUM_READY_TIMEOUT_SECONDS,
        )
        if browser.poll() is not None:
            raise WindowsBrowserE2EError("Windows Chromium exited during startup")

        phase = "agent-start"
        agent = windows._spawn(
            windows.agent_command(server_port, workspace),
            environment=agent_environment,
            cwd=repository,
            lifecycle=lifecycle,
            diagnostic_file=diagnostics / "agent.stderr.log",
        )

        def agent_ready() -> bool:
            if agent.poll() is not None:
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
        phase = "cdp-screenshot"
        capture_screenshot(
            f"http://127.0.0.1:{cdp_port}",
            fixture_url,
            evidence_dir,
        )
        if server.poll() is not None or fixture.poll() is not None or browser.poll() is not None:
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
                print(
                    f"Windows Browser E2E cleanup phase: {label}.",
                    file=sys.stderr,
                )

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
                windows.write_artifact(output_file.parent, output_file.name, (line + "\n").encode("ascii"))
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

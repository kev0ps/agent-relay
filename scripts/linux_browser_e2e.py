#!/usr/bin/env python3
"""Run the bounded native Linux Browser/CDP Agent Relay smoke scenario."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import importlib.util
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).parents[1].resolve()
FIXTURE_SCRIPT = ROOT / "tests" / "fixtures" / "browser_app.py"


def _load_module(name: str, path: Path) -> Any:
    dotted = f"_agent_relay_linux_browser_{name}"
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


native = _load_module("native_e2e", Path(__file__).with_name("native_e2e.py"))
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


DEVICE_ID = "linux-browser-e2e-agent"
BROWSER_CAPABILITIES = (
    "browser.click",
    "browser.fill",
    "browser.list_tabs",
    "browser.navigate",
    "browser.read_page",
    "system.ping",
    "terminal.exec",
)
FIXTURE_READY_TIMEOUT_SECONDS = 15.0
CHROMIUM_READY_TIMEOUT_SECONDS = 30.0
AGENT_READY_TIMEOUT_SECONDS = 30.0
MAX_CDP_FRAME_BYTES = 1024 * 1024
MAX_SCREENSHOT_BYTES = 512 * 1024
MAX_SCREENSHOT_DIMENSION = 4096

LinuxBrowserE2EError = native.NativeE2EError


def chromium_command(executable: Path, cdp_port: int, profile: Path) -> list[str]:
    """Return the fixed headless Chromium command used by the smoke."""
    native._validate_port(cdp_port)
    if not isinstance(executable, Path) or not isinstance(profile, Path):
        raise ValueError("Chromium executable and profile must be Paths")
    if not executable.is_absolute() or not profile.is_absolute():
        raise ValueError("Chromium executable and profile must be absolute")
    return [
        str(executable),
        "--headless=new",
        # GitHub-hosted Ubuntu disables Chromium's unprivileged user namespace
        # sandbox; this process only visits the loopback fixture in an ephemeral job.
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
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
    native._validate_port(port)
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
    """Resolve the pinned Playwright Chromium executable or a fixed Linux path."""
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
    candidates.extend(
        Path(path)
        for path in ("/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome")
    )
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise LinuxBrowserE2EError("Chromium executable is unavailable")


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
        raise LinuxBrowserE2EError("Chromium page listing failed") from error
    if not isinstance(payload, list) or not 1 <= len(payload) <= 8:
        raise LinuxBrowserE2EError("Chromium page listing is invalid")
    matches = [
        item
        for item in payload
        if isinstance(item, dict)
        and item.get("type") == "page"
        and item.get("url") == fixture_url
        and isinstance(item.get("webSocketDebuggerUrl"), str)
    ]
    if len(matches) != 1:
        raise LinuxBrowserE2EError("Chromium fixture page identity is invalid")
    return matches[0]["webSocketDebuggerUrl"]


async def _capture_png(ws_url: str) -> bytes:
    try:
        import websockets
    except ImportError as error:
        raise LinuxBrowserE2EError("WebSocket client is unavailable") from error
    async with websockets.connect(
        ws_url,
        open_timeout=2,
        close_timeout=2,
        max_size=MAX_CDP_FRAME_BYTES,
    ) as socket:
        await socket.send(
            json.dumps(
                {"id": 1, "method": "Page.captureScreenshot", "params": {"format": "png"}},
                separators=(",", ":"),
            )
        )
        while True:
            raw = await asyncio.wait_for(socket.recv(), timeout=5)
            if not isinstance(raw, str):
                raise LinuxBrowserE2EError("Chromium CDP returned binary framing")
            message = json.loads(raw)
            if not isinstance(message, dict) or message.get("id") != 1:
                continue
            if set(message) - {"id", "result", "error"}:
                raise LinuxBrowserE2EError("Chromium CDP returned extra fields")
            if "error" in message:
                raise LinuxBrowserE2EError("Chromium screenshot request failed")
            result = message.get("result")
            if not isinstance(result, dict) or set(result) != {"data"}:
                raise LinuxBrowserE2EError("Chromium screenshot result is invalid")
            data = result["data"]
            if not isinstance(data, str) or len(data) > MAX_SCREENSHOT_BYTES * 2:
                raise LinuxBrowserE2EError("Chromium screenshot is oversized")
            try:
                image = base64.b64decode(data, validate=True)
            except (binascii.Error, ValueError) as error:
                raise LinuxBrowserE2EError("Chromium screenshot encoding is invalid") from error
            validate_screenshot_png(image)
            return image


def validate_screenshot_png(payload: bytes) -> tuple[int, int]:
    if not isinstance(payload, bytes) or not 24 <= len(payload) <= MAX_SCREENSHOT_BYTES:
        raise LinuxBrowserE2EError("Chromium screenshot is not a bounded PNG")
    if payload[:8] != b"\x89PNG\r\n\x1a\n" or payload[12:16] != b"IHDR":
        raise LinuxBrowserE2EError("Chromium screenshot is not a bounded PNG")
    width = int.from_bytes(payload[16:20], "big")
    height = int.from_bytes(payload[20:24], "big")
    if not 1 <= width <= MAX_SCREENSHOT_DIMENSION or not 1 <= height <= MAX_SCREENSHOT_DIMENSION:
        raise LinuxBrowserE2EError("Chromium screenshot dimensions are invalid")
    return width, height


def _write_screenshot(evidence_dir: Path, payload: bytes) -> None:
    validate_screenshot_png(payload)
    if evidence_dir.is_symlink():
        raise LinuxBrowserE2EError("unsafe evidence directory")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    target = evidence_dir / "screenshot.png"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError:
        raise LinuxBrowserE2EError("screenshot artifact already exists") from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise LinuxBrowserE2EError("unsafe screenshot artifact")
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def capture_screenshot(cdp_url: str, fixture_url: str, evidence_dir: Path | None) -> None:
    image = asyncio.run(_capture_png(_fixture_page_socket(cdp_url, fixture_url)))
    if evidence_dir is not None:
        _write_screenshot(evidence_dir, image)


def _runtime(*, mcp_url: str, control_token: str, run_id: str, fixtures_root: Path, fixture_url: str) -> Any:
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


def _stderr_hint(path: Path) -> str | None:
    """Return a short, redacted diagnostic line without exposing child logs."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    candidates = [
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


def run_scenario(evidence_dir: Path | None = None, *, output_file: Path | None = None) -> None:
    """Run the native Linux Browser/CDP scenario with bounded cleanup."""
    if sys.platform != "linux":
        raise LinuxBrowserE2EError("native Linux Browser harness requires Linux")

    agent_token, control_token = native.generate_credentials()
    server_port = native.choose_loopback_port()
    fixture_port = native.choose_loopback_port()
    cdp_port = native.choose_loopback_port()
    run_id = f"linux-browser-{secrets.token_hex(12)}"
    value = f"relay-gh-browser-{run_id}"
    phase = "setup"
    scenario_phase: list[str] = []
    lifecycle = native.NativeLifecycle()
    scenario_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    diagnostics: dict[str, Path] = {}

    try:
        lifecycle.install_signal_handlers()
        temporary = tempfile.TemporaryDirectory(prefix="agent-relay-linux-browser-")
        lifecycle.add_cleanup(temporary.cleanup)
        root = Path(temporary.name)
        home = root / "home"
        workspace = root / "workspace"
        profile = root / "chromium-profile"
        local_artifacts = evidence_dir or (root / "browser-evidence")
        diagnostics["Chromium"] = root / "chromium.stderr.log"
        for path in (home, workspace, profile, local_artifacts):
            path.mkdir(parents=True, exist_ok=True)
        repository = ROOT
        fixture_url = f"http://127.0.0.1:{fixture_port}/"
        mcp_url = f"http://127.0.0.1:{server_port}/mcp"
        chromium = resolve_chromium_executable()
        server_environment = native._minimal_environment(
            home,
            {
                "AGENT_RELAY_DEVICE_ID": DEVICE_ID,
                "AGENT_RELAY_AGENT_TOKEN": agent_token,
                "AGENT_RELAY_CONTROL_TOKEN": control_token,
                "AGENT_RELAY_PORT": str(server_port),
            },
        )
        agent_environment = native._minimal_environment(
            home,
            {
                "AGENT_RELAY_DEVICE_ID": DEVICE_ID,
                "AGENT_RELAY_AGENT_TOKEN": agent_token,
                "AGENT_RELAY_SERVER_URL": f"ws://127.0.0.1:{server_port}/ws/agent",
                "AGENT_RELAY_WORKSPACE": str(workspace),
                "AGENT_RELAY_HEARTBEAT_INTERVAL_SECONDS": "0.2",
                "AGENT_RELAY_NATIVE_DEBUG": "1",
                "AGENT_RELAY_BROWSER_CDP_URL": f"http://127.0.0.1:{cdp_port}",
                "AGENT_RELAY_BROWSER_ALLOWED_ORIGINS": f"http://127.0.0.1:{fixture_port}",
            },
        )
        fixture_environment = native._minimal_environment(home, {"ARTIFACTS_DIR": str(local_artifacts)})
        chromium_environment = native._minimal_environment(home, {})
        runtime = _runtime(
            mcp_url=mcp_url,
            control_token=control_token,
            run_id=run_id,
            fixtures_root=local_artifacts,
            fixture_url=fixture_url,
        )

        phase = "server-start"
        server = native._spawn(native.server_command(server_port), environment=server_environment, cwd=repository, lifecycle=lifecycle)
        native._wait_for("Linux Browser server", lambda: _status(mcp_url, control_token, connected=False) is None, timeout=native.SERVER_READY_TIMEOUT_SECONDS)
        if server.poll() is not None:
            raise LinuxBrowserE2EError("Linux Browser server exited during startup")

        phase = "fixture-start"
        fixture = native._spawn(fixture_command(fixture_port, run_id), environment=fixture_environment, cwd=repository, lifecycle=lifecycle)
        native._wait_for("Linux Browser fixture", lambda: _fixture_ready(fixture_url), timeout=FIXTURE_READY_TIMEOUT_SECONDS)
        if fixture.poll() is not None:
            raise LinuxBrowserE2EError("Linux Browser fixture exited during startup")

        phase = "chromium-start"
        browser = native._spawn(
            chromium_command(chromium, cdp_port, profile),
            environment=chromium_environment,
            cwd=repository,
            lifecycle=lifecycle,
            stderr_path=diagnostics["Chromium"],
        )
        native._wait_for("Linux Chromium CDP", lambda: _cdp_ready(f"http://127.0.0.1:{cdp_port}"), timeout=CHROMIUM_READY_TIMEOUT_SECONDS)
        if browser.poll() is not None:
            raise LinuxBrowserE2EError("Linux Chromium exited during startup")

        phase = "agent-start"
        agent = native._spawn(native.agent_command(server_port, workspace), environment=agent_environment, cwd=repository, lifecycle=lifecycle)

        def agent_ready() -> bool:
            if agent.poll() is not None:
                raise LinuxBrowserE2EError("Linux Browser Agent exited during startup")
            _status(mcp_url, control_token, connected=True)
            return True

        native._wait_for("Linux Browser Agent registration", agent_ready, timeout=AGENT_READY_TIMEOUT_SECONDS)
        if agent.poll() is not None:
            raise LinuxBrowserE2EError("Linux Browser Agent exited after registration")

        phase = "browser-scenario"
        portable_scenarios.run_browser_scenario(runtime, value, scenario_phase, expected_capabilities=BROWSER_CAPABILITIES)
        phase = "cdp-screenshot"
        capture_screenshot(f"http://127.0.0.1:{cdp_port}", fixture_url, evidence_dir)
        if any(process.poll() is not None for process in (server, fixture, browser, agent)):
            raise LinuxBrowserE2EError("Linux Browser owned process exited unexpectedly")
    except BaseException as error:
        scenario_error = error
        for label, path in diagnostics.items():
            if (hint := _stderr_hint(path)) is not None:
                print(f"Linux Browser {label} diagnostic: {hint}", file=sys.stderr)

    primary_error: BaseException | None = scenario_error
    if not lifecycle._cleaned:
        try:
            lifecycle.cleanup()
        except BaseException as error:
            cleanup_error = error

    primary_error = scenario_error or cleanup_error
    if primary_error is None:
        try:
            if output_file is not None:
                native._write_artifact(output_file.parent, output_file.name, b"Linux Browser smoke scenario passed.\n")
            if evidence_dir is not None:
                native._write_success(evidence_dir)
        except BaseException as error:
            primary_error = error

    if primary_error is not None:
        detail = f": {primary_error}" if isinstance(primary_error, LinuxBrowserE2EError) else f": {type(primary_error).__name__}"
        if scenario_phase:
            detail += f" (phase-{scenario_phase[-1]})"
        line = f"Linux Browser E2E failed at scenario-{phase}{detail}."
        print(line, file=sys.stderr)
        if scenario_error is not None and cleanup_error is not None:
            print("Linux Browser E2E cleanup failed.", file=sys.stderr)
        if output_file is not None:
            try:
                native._write_artifact(output_file.parent, output_file.name, (line + "\n").encode("ascii"))
            except BaseException:
                print("Linux Browser E2E artifact write failed.", file=sys.stderr)
        raise primary_error
    print("Linux Browser smoke scenario passed.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Native Linux Browser Agent Relay smoke")
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

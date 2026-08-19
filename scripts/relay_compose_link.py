#!/usr/bin/env python3
"""Smoke-test the Docker Compose Server with one native Linux Agent."""

from __future__ import annotations

import argparse
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    from tests.e2e import mcp_client as portable_mcp
    from tests.e2e import oracles as portable_oracles
except ModuleNotFoundError as error:
    if error.name not in {"tests", "tests.e2e"}:
        raise

    import importlib.util

    def _load_portable(name: str) -> Any:
        dotted = f"_agent_relay_compose_smoke_{name}"
        cached = sys.modules.get(dotted)
        if cached is not None:
            return cached
        target = Path(__file__).parents[1] / "tests" / "e2e" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(dotted, target)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load portable MCP module {name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[dotted] = module
        spec.loader.exec_module(module)
        return module

    portable_mcp = _load_portable("mcp_client")
    portable_oracles = _load_portable("oracles")


DEVICE_ID = "compose-status-smoke-agent"
MCP_URL = "http://127.0.0.1:8000/mcp"
RELAY_URL = "ws://127.0.0.1:8000/ws/agent"
MAX_TOKEN_LENGTH = 128
POLL_INTERVAL_SECONDS = 0.2
AGENT_READY_TIMEOUT_SECONDS = 30.0
PROCESS_STOP_TIMEOUT_SECONDS = 5.0


class ComposeSmokeError(RuntimeError):
    """A bounded, non-sensitive Compose smoke-test failure."""


def _read_private_token(path: Path) -> str:
    """Read one bounded regular token file without exposing its contents."""
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise ComposeSmokeError("token file is not a regular file")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ComposeSmokeError("token file permissions are too broad")
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise ComposeSmokeError("token file is unavailable") from None
    if not value or len(value) > MAX_TOKEN_LENGTH or any(char.isspace() for char in value):
        raise ComposeSmokeError("token file is invalid")
    return value


def _minimal_environment(home: Path, values: dict[str, str]) -> dict[str, str]:
    """Build an Agent environment without inheriting unrelated secrets."""
    environment = {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "PATH": os.environ.get("PATH", ""),
        "PYTHONUNBUFFERED": "1",
    }
    if not os.environ.get("RELAY_E2E_AGENT_RELAY_COMMAND"):
        environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    environment.update(values)
    return environment


def _spawn_agent(workspace: Path, token: str, home: Path) -> subprocess.Popen[Any]:
    """Start the installed or source Agent command in an owned process group."""
    installed_command = os.environ.get("RELAY_E2E_AGENT_RELAY_COMMAND")
    command = (
        [installed_command, "agent"]
        if installed_command
        else [sys.executable, "-m", "agent_relay.agent"]
    )
    environment = _minimal_environment(
        home,
        {
            "RELAY_URL": RELAY_URL,
            "RELAY_AGENT_TOKEN": token,
            "RELAY_AGENT_ID": DEVICE_ID,
            "RELAY_AGENT_WORKSPACE": str(workspace),
            "RELAY_AGENT_HEARTBEAT_INTERVAL_SECONDS": "0.2",
        },
    )
    return subprocess.Popen(
        command,
        cwd=Path(__file__).parents[1],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        shell=False,
    )


def _stop_agent(process: subprocess.Popen[Any]) -> None:
    """Terminate the owned Agent process group within a bounded timeout."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise ComposeSmokeError("Agent cleanup timed out") from None


def _wait_for_connected_status(control_token: str) -> None:
    """Wait until the one allowed MCP status call sees the Agent online."""
    deadline = time.monotonic() + AGENT_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            result = portable_mcp.call_tool(
                MCP_URL,
                control_token,
                "relay_device_status",
                {},
                http_timeout=1.0,
                operation_timeout=2.0,
            )
            portable_oracles.validate_status(
                result,
                device_id=DEVICE_ID,
                connected=True,
                expected_capabilities=(),
            )
            return
        except (ConnectionError, ValueError):
            time.sleep(POLL_INTERVAL_SECONDS)
    raise ComposeSmokeError("timed out waiting for connected relay status")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp-token-file", type=Path, required=True)
    parser.add_argument("--agent-token-file", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    if os.name != "posix":
        print("Compose smoke test requires Linux", file=sys.stderr)
        return 1
    args = _parse_args()
    process: subprocess.Popen[Any] | None = None
    try:
        control_token = _read_private_token(args.mcp_token_file)
        agent_token = _read_private_token(args.agent_token_file)
        args.workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        args.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        process = _spawn_agent(args.workspace, agent_token, args.home)
        _wait_for_connected_status(control_token)
    except (ComposeSmokeError, OSError, ValueError) as error:
        print(f"Compose status smoke failed: {error}", file=sys.stderr)
        return 1
    finally:
        if process is not None:
            try:
                _stop_agent(process)
            except ComposeSmokeError as error:
                print(f"Compose status smoke cleanup failed: {error}", file=sys.stderr)
                if process.returncode is None:
                    return 1
    print("Relay Compose Link passed: relay_device_status connected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

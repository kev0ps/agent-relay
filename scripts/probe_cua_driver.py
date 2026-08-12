"""Bounded MCP startup probes for the native CUA CI gates."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import time
from collections.abc import Sequence

from agent_relay.capabilities.computer import (
    _driver_stderr_category,
    _driver_stderr_line_category,
    get_cua_driver_path,
    safe_driver_environment,
    windows_daemon_pipe_ready,
)

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "ci-probe", "version": "1"},
    },
}
INITIALIZED = {
    "jsonrpc": "2.0",
    "method": "notifications/initialized",
    "params": {},
}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


def _message(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


def _result_message(
    line: bytes, *, require_id: bool = True
) -> dict[str, object] | None:
    try:
        message = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        type(message) is dict
        and "result" in message
        and (not require_id or "id" in message)
    ):
        return message
    return None


async def _probe_linux(driver: str, probe_env: dict[str, str]) -> int:
    process = await asyncio.wait_for(
        asyncio.create_subprocess_exec(
            driver,
            "mcp",
            "--no-overlay",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=probe_env,
            start_new_session=True,
            limit=4 * 1024 * 1024 + 1,
        ),
        5.0,
    )
    print("cua-driver mcp probe: spawn=ok", flush=True)
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(_message(INITIALIZE))
        await process.stdin.drain()
        line = await asyncio.wait_for(process.stdout.readline(), 5.0)
        if not line:
            print("cua-driver mcp probe: initialize=closed", flush=True)
            return 1
        message = _result_message(line, require_id=False)
        if message is None:
            print("cua-driver mcp probe: initialize=error", flush=True)
            return 1
        print("cua-driver mcp probe: initialize=result", flush=True)

        process.stdin.write(_message(INITIALIZED))
        process.stdin.write(_message(TOOLS_LIST))
        await process.stdin.drain()
        line = await asyncio.wait_for(process.stdout.readline(), 5.0)
        if not line:
            print("cua-driver mcp probe: tools-list=closed", flush=True)
            return 1
        message = _result_message(line, require_id=False)
        if message is None:
            print("cua-driver mcp probe: tools-list=error", flush=True)
            return 1
        print("cua-driver mcp probe: tools-list=result", flush=True)
        return 0
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 2.0)
            except asyncio.TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()


def _probe_windows(driver: str, probe_env: dict[str, str]) -> int:
    payload = _message(INITIALIZE) + _message(INITIALIZED) + _message(TOOLS_LIST)
    daemon = subprocess.Popen(
        [driver, "serve", "--no-overlay", "--no-permissions-gate"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=probe_env,
    )
    process: subprocess.Popen[bytes] | None = None
    try:
        deadline = time.monotonic() + 15
        while not windows_daemon_pipe_ready():
            if daemon.poll() is not None:
                print("windows cua mcp probe: daemon-failure")
                return 1
            if time.monotonic() >= deadline:
                print("windows cua mcp probe: daemon-timeout")
                return 1
            time.sleep(0.05)
        process = subprocess.Popen(
            [driver, "mcp", "--no-overlay"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=probe_env,
        )
        try:
            stdout, stderr = process.communicate(input=payload, timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            print("windows cua mcp probe: process-timeout")
            return 1
        if process.returncode != 0:
            categories = {
                category
                for line in stderr.splitlines()
                if (category := _driver_stderr_line_category(line))
            }
            category = _driver_stderr_category(categories, bool(stderr.strip()))
            print(
                "windows cua mcp probe: process-failure "
                f"category={category or 'unknown'}"
            )
            return 1
        responses = [
            message
            for line in stdout.splitlines()
            if (message := _result_message(line)) is not None
        ]
        if {message["id"] for message in responses} != {1, 2}:
            print("windows cua mcp probe: response-failure")
            return 1
        print(
            "windows cua mcp probe: daemon-ready mcp=ok "
            "initialize=result tools-list=result"
        )
        return 0
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        if daemon.poll() is None:
            daemon.terminate()
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=("linux", "windows"))
    arguments = parser.parse_args(argv)
    driver = str(get_cua_driver_path())
    probe_env = safe_driver_environment(os.environ.copy())
    if arguments.platform == "linux":
        return asyncio.run(_probe_linux(driver, probe_env))
    return _probe_windows(driver, probe_env)


if __name__ == "__main__":
    raise SystemExit(main())

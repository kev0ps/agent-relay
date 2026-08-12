from __future__ import annotations

import asyncio
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

from agent_relay.capabilities.computer import (
    ComputerCapability,
    _driver_stderr_category,
    _driver_stderr_line_category,
    get_cua_driver_path,
    safe_driver_environment,
    validate_driver_executable,
)


def _write_driver(path: Path) -> None:
    path.write_text(
        """
import json
import sys

tools = [
    "list_windows",
    "browser_prepare",
    "browser_navigate",
    "browser_click",
    "browser_type",
    "execute_javascript",
]

def emit(message):
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    request_id = request["id"]
    if method == "initialize":
        emit({"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "serverInfo": {"name": "fixture-cua", "version": "1"},
        }})
    elif method == "tools/list":
        emit({"jsonrpc": "2.0", "id": request_id, "result": {
            "tools": [
                {"name": name, "description": "fixture tool", "inputSchema": {
                    "type": "object", "properties": {
                        "url": {"type": "string", "maxLength": 256}
                    }, "additionalProperties": False
                }} for name in tools
            ]
        }})
    elif method == "tools/call":
        name = request["params"]["name"]
        emit({"jsonrpc": "2.0", "id": request_id, "result": {
            "content": [{"type": "text", "text": "provider-result"}],
            "structuredContent": {"tool": name, "arguments": request["params"].get("arguments", {})},
            "isError": False,
        }})
""".strip()
        + "\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_get_cua_driver_path_uses_only_the_package_api(
    tmp_path: Path, monkeypatch
) -> None:
    executable = tmp_path / "cua-driver"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setitem(
        sys.modules,
        "cua_driver",
        SimpleNamespace(get_binary_path=lambda: str(executable)),
    )

    assert get_cua_driver_path() == executable


def test_cua_capability_discovers_and_calls_native_and_browser_tools(
    tmp_path: Path, monkeypatch
) -> None:
    executable = tmp_path / "cua-driver.py"
    _write_driver(executable)
    monkeypatch.setattr(
        "agent_relay.capabilities.computer.get_cua_driver_path",
        lambda: executable,
    )
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def spawn_driver(program, *arguments, **kwargs):
        if Path(program) == executable:
            return await real_create_subprocess_exec(
                sys.executable,
                str(executable),
                *arguments,
                **kwargs,
            )
        return await real_create_subprocess_exec(program, *arguments, **kwargs)

    monkeypatch.setattr(
        "agent_relay.capabilities.computer.asyncio.create_subprocess_exec",
        spawn_driver,
    )

    async def scenario() -> None:
        capability = ComputerCapability(startup_timeout_seconds=2, action_timeout_seconds=2)
        capability._windows = False
        await capability.start()
        descriptors = await capability.list_tools()
        assert {descriptor.tool_name for descriptor in descriptors} == {
            "list_windows",
            "browser_prepare",
            "browser_navigate",
            "browser_click",
            "browser_type",
            "execute_javascript",
        }

        native_result = await capability.call_tool("browser_prepare", {})
        browser_result = await capability.call_tool(
            "browser_navigate", {"url": "http://127.0.0.1/"}
        )
        assert native_result.structured_content["tool"] == "browser_prepare"
        assert browser_result.structured_content == {
            "tool": "browser_navigate",
            "arguments": {"url": "http://127.0.0.1/"},
        }
        await capability.aclose()

    asyncio.run(scenario())


def test_driver_diagnostics_are_closed_and_safe() -> None:
    assert _driver_stderr_line_category(b"named pipe failed: secret") == "named-pipe"
    assert _driver_stderr_line_category(b"access is denied") == "permission"
    assert _driver_stderr_line_category(b"unclassified secret") == "driver-error"
    assert _driver_stderr_category({"daemon", "named-pipe"}, True) == "named-pipe"
    assert _driver_stderr_category(set(), False) is None


def test_driver_environment_excludes_relay_credentials() -> None:
    environment = safe_driver_environment(
        {
            "PATH": "/usr/bin",
            "RELAY_AGENT_TOKEN": "secret",
            "RELAY_URL": "ws://localhost",
            "CUA_DRIVER_RS_HOME": "/tmp/cua",
        }
    )
    assert environment["PATH"] == "/usr/bin"
    assert environment["CUA_DRIVER_TELEMETRY"] == "0"
    assert "RELAY_AGENT_TOKEN" not in environment
    assert "RELAY_URL" not in environment


def test_driver_executable_validation_is_independent_of_configuration_fields(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "driver"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    assert validate_driver_executable(executable) == executable

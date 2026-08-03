from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

import pytest

from agent_relay.capabilities.computer import (
    ComputerCapability,
    ComputerUnavailableError,
    safe_driver_environment,
    validate_driver_executable,
    validate_windows_health,
)
from agent_relay.catalog import CUA_REFERENCE_TOOL_NAMES
from agent_relay.output_models import ProviderTextContent
from agent_relay.providers.base import ProviderTimeoutError, ProviderToolError

_GENERIC_DRIVER = r'''#!/usr/bin/env python3
import json
import os
import sys
import time

TOOLS = __TOOLS__
MODE = __MODE__
LOG = __LOG__


def log(value):
    with open(LOG, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, separators=(",", ":")) + "\n")


def emit(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def schema(name):
    if MODE == "bad_schema" and name == "click":
        return {"type": "evil"}
    if name == "click":
        return {
            "type": "object",
            "properties": {"target": {"type": "string", "minLength": 1}},
            "required": ["target"],
            "additionalProperties": False,
        }
    if name == "type_text":
        return {
            "type": "object",
            "properties": {
                "target": {"type": "string", "minLength": 1},
                "text": {"type": "string", "minLength": 1, "maxLength": 128},
            },
            "required": ["target", "text"],
            "additionalProperties": False,
        }
    return {"type": "object", "properties": {}, "additionalProperties": False}


args = sys.argv[1:]
if args != ["mcp", "--no-overlay"]:
    log({"argv": args, "env": dict(os.environ)})
    if args == ["telemetry", "status", "--json"]:
        sys.stdout.write(json.dumps({"enabled": False, "installation_id_present": False}) + "\n")
        sys.stdout.flush()
    raise SystemExit(0)

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    log(request)
    request_id = request["id"]
    method = request["method"]
    if MODE == "wrong_id" and method == "initialize":
        request_id += 1
    if MODE == "oversized" and method == "initialize":
        sys.stdout.write("x" * 300000 + "\n")
        sys.stdout.flush()
        continue
    if MODE == "malformed" and method == "tools/list":
        sys.stdout.write("not-json\n")
        sys.stdout.flush()
        continue
    version = "1.0" if MODE == "wrong_version" else "2.0"
    if method == "initialize":
        emit({"jsonrpc": version, "id": request_id, "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fixture-cua", "version": "1"},
        }})
    elif method == "tools/list":
        emit({"jsonrpc": "2.0", "id": request_id, "result": {
            "tools": [
                {"name": name, "description": "fixture tool", "inputSchema": schema(name)}
                for name in TOOLS
            ]
        }})
    elif method == "tools/call":
        name = request["params"]["name"]
        if MODE == "hang":
            time.sleep(10)
        elif MODE == "exit":
            raise SystemExit(7)
        elif MODE == "raw_error":
            emit({"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32000, "message": "backend secret must not escape"
            }})
        else:
            emit({"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": "provider-result"}],
                "structuredContent": {
                    "tool": name, "arguments": request["params"].get("arguments", {})
                },
                "isError": False,
            }})
'''


def _write_driver(tmp_path: Path, *, mode: str = "normal", extra_tool: str | None = None) -> tuple[Path, Path]:
    log = tmp_path / "driver.log"
    tools = list(CUA_REFERENCE_TOOL_NAMES)
    if extra_tool is not None:
        tools.append(extra_tool)
    script = (
        _GENERIC_DRIVER.replace("__TOOLS__", repr(tools))
        .replace("__MODE__", repr(mode))
        .replace("__LOG__", repr(str(log)))
    )
    path = tmp_path / "cua-driver"
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path, log


def _configured(path: Path, *, action_timeout: float = 1, **kwargs: object) -> ComputerCapability:
    return ComputerCapability(
        path,
        "Fixture",
        "Relay Desktop Fixture",
        startup_timeout_seconds=2,
        action_timeout_seconds=action_timeout,
        shutdown_timeout_seconds=1,
        **kwargs,
    )


def test_cua_reference_inventory_contains_exactly_fifty_generic_names() -> None:
    assert len(CUA_REFERENCE_TOOL_NAMES) == 50
    assert len(set(CUA_REFERENCE_TOOL_NAMES)) == 50
    assert ComputerCapability.tools == frozenset(
        f"cua.{name}" for name in CUA_REFERENCE_TOOL_NAMES
    )


def test_computer_capability_lists_and_calls_provider_native_tools(tmp_path: Path) -> None:
    async def scenario() -> None:
        path, log = _write_driver(tmp_path)
        capability = _configured(path)
        await capability.start()
        descriptors = await capability.list_tools()
        assert len(descriptors) == 50
        assert {item.tool_name for item in descriptors} == set(CUA_REFERENCE_TOOL_NAMES)

        result = await capability.call_tool("click", {"target": "opaque-target"})
        assert isinstance(result.content[0], ProviderTextContent)
        assert result.content[0].text == "provider-result"
        assert result.structured_content == {
            "tool": "click",
            "arguments": {"target": "opaque-target"},
        }

        calls = [json.loads(line) for line in log.read_text().splitlines()]
        call = next(item for item in calls if item.get("method") == "tools/call")
        assert call["params"] == {
            "name": "click",
            "arguments": {"target": "opaque-target"},
        }
        await capability.aclose()

    asyncio.run(scenario())


def test_provider_arguments_are_validated_before_tools_call(tmp_path: Path) -> None:
    async def scenario() -> None:
        path, log = _write_driver(tmp_path)
        capability = _configured(path)
        await capability.start()
        before = len(
            [
                line
                for line in log.read_text().splitlines()
                if '"method":"tools/call"' in line
            ]
        )
        with pytest.raises(ProviderToolError):
            await capability.call_tool("click", {})
        with pytest.raises(ProviderToolError):
            await capability.call_tool("click", {"target": "ok", "extra": True})
        after = len(
            [
                line
                for line in log.read_text().splitlines()
                if '"method":"tools/call"' in line
            ]
        )
        assert before == after
        await capability.aclose()

    asyncio.run(scenario())


def test_provider_inventory_accepts_a_fifty_first_tool_without_relay_edit(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path, _ = _write_driver(tmp_path, extra_tool="provider_added_later")
        capability = _configured(path)
        await capability.start()
        descriptors = await capability.list_tools()
        assert len(descriptors) == 51
        assert any(item.tool_name == "provider_added_later" for item in descriptors)
        await capability.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mode", ["wrong_id", "wrong_version", "malformed", "oversized", "bad_schema"]
)
def test_startup_protocol_failures_are_fail_closed(tmp_path: Path, mode: str) -> None:
    async def scenario() -> None:
        path, _ = _write_driver(tmp_path, mode=mode)
        capability = _configured(path)
        with pytest.raises(ComputerUnavailableError):
            await capability.start()
        await asyncio.wait_for(capability.wait_unavailable(), timeout=1)
        await capability.aclose()

    asyncio.run(scenario())


def test_provider_error_is_safe_and_closes_idempotently(tmp_path: Path) -> None:
    async def scenario() -> None:
        path, _ = _write_driver(tmp_path, mode="raw_error")
        capability = _configured(path)
        await capability.start()
        with pytest.raises(ProviderToolError) as error:
            await capability.call_tool("click", {"target": "opaque-target"})
        assert "backend secret" not in str(error.value)
        await capability.aclose()
        await capability.aclose()
        await asyncio.wait_for(capability.wait_unavailable(), timeout=1)

    asyncio.run(scenario())


def test_cancellation_terminates_owned_provider_process(tmp_path: Path) -> None:
    async def scenario() -> None:
        path, _ = _write_driver(tmp_path, mode="hang")
        capability = _configured(path, action_timeout=10)
        await capability.start()
        process = capability._process
        assert process is not None
        task = asyncio.create_task(
            capability.call_tool("click", {"target": "opaque-target"})
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(capability.wait_unavailable(), timeout=1)
        await capability.aclose()
        assert process.returncode is not None

    asyncio.run(scenario())


def test_timeout_terminates_owned_provider_process(tmp_path: Path) -> None:
    async def scenario() -> None:
        path, _ = _write_driver(tmp_path, mode="hang")
        capability = _configured(path, action_timeout=0.05)
        await capability.start()
        process = capability._process
        assert process is not None
        with pytest.raises(ProviderTimeoutError):
            await capability.call_tool("click", {"target": "opaque-target"})
        assert process.returncode is not None
        await asyncio.wait_for(capability.wait_unavailable(), timeout=1)
        await capability.aclose()

    asyncio.run(scenario())


def test_safe_driver_environment_excludes_relay_credentials() -> None:
    environment = safe_driver_environment(
        {
            "PATH": "/usr/bin",
            "HOME": "/tmp/home",
            "AGENT_RELAY_AGENT_TOKEN": "[REDACTED]",
            "HTTPS_PROXY": "http://proxy.invalid",
            "CUA_DRIVER_RS_TELEMETRY_ENABLED": "1",
        }
    )
    assert environment == {
        "PATH": "/usr/bin",
        "HOME": "/tmp/home",
        "CUA_DRIVER_TELEMETRY": "0",
        "CUA_DRIVER_RS_TELEMETRY_ENABLED": "0",
    }


def test_no_operation_specific_cua_dispatch_remains() -> None:
    source = Path(__file__).parents[1].joinpath(
        "src", "agent_relay", "capabilities", "computer.py"
    ).read_text(encoding="utf-8")
    assert "computer.capture" not in source
    assert "computer.click" not in source
    assert "computer.type" not in source
    assert "element_id" not in source


def test_driver_path_validation_rejects_relative_and_symlink(tmp_path: Path) -> None:
    relative = Path("cua-driver")
    with pytest.raises(ValueError):
        validate_driver_executable(relative)
    target = tmp_path / "driver"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o755)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        validate_driver_executable(link)


def test_windows_health_requires_all_required_checks() -> None:
    payload = {
        "schema_version": "1",
        "platform": "win32",
        "overall": "ok",
        "checks": [
            {"name": name, "status": "pass", "message": "ok"}
            for name in (
                "binary_version",
                "platform_supported",
                "session_active",
                "ax_capability",
            )
        ],
    }
    validate_windows_health(payload)
    payload["checks"][-1]["status"] = "fail"
    with pytest.raises(ValueError):
        validate_windows_health(payload)

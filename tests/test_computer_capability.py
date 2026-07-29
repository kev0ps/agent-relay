from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from agent_relay.capabilities.computer import (
    ComputerCapability,
    ComputerUnavailableError,
    safe_driver_environment,
    validate_driver_executable,
)
from agent_relay.protocol import (
    ComputerCaptureInvoke,
    ComputerClickInvoke,
    ComputerTypeInvoke,
)

FAKE = r"""#!/usr/bin/env python3
import json, os, subprocess, sys, time
mode=os.environ.get("FAKE_MODE", "normal")
log=os.environ.get("FAKE_LOG")
def record(value):
    if log:
        with open(log,"a") as stream: stream.write(json.dumps(value,separators=(",",":"))+"\n")
argv=sys.argv[1:]
if argv == ["mcp", "--no-overlay"]:
    record({"mcp_argv":argv,"env":dict(os.environ)})
elif argv:
    record({"argv":argv,"env":dict(os.environ)})
    if argv==["telemetry","status","--json"]:
        if mode=="telemetry_oversized":
            sys.stdout.write("x"*300000); sys.stdout.flush(); time.sleep(10)
        if mode=="telemetry_invalid_json":
            print("{")
            sys.exit(0)
        print(json.dumps({"enabled": mode=="telemetry_on", "installation_id_present":False}))
    sys.exit(0)
else:
    sys.exit(9)
schemas={
 "start_session":{"session"}, "list_windows":{"on_screen_only","pid"},
 "get_window_state":{"session","pid","window_id","capture_mode","include_screenshot","screenshot_out_file","query","max_elements","max_depth"},
 "click":{"session","cursor_id","pid","window_id","x","y","element_index","element_token","button","count","from_zoom","delivery_mode"},
 "type_text":{"session","pid","window_id","text","element_index","element_token","x","y","delivery_mode"},
 "end_session":{"session"}}
required={"start_session":{"session"},"list_windows":set(),
 "get_window_state":{"pid","window_id"},
 "click":set(),
 "type_text":{"pid","text"},
 "end_session":{"session"}}
additional={"start_session":True,"end_session":True}
calls=0
for line in sys.stdin:
    request=json.loads(line); record(request)
    if "id" not in request: continue
    ident=request["id"]
    if mode=="wrong_id" and request["method"]=="initialize": ident+=1
    if mode=="hang" and request.get("method")=="tools/call" and request["params"]["name"]=="click":
        child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(10)"])
        record({"child_pid":child.pid}); time.sleep(10)
    if mode=="exit" and request.get("method")=="tools/call" and request["params"]["name"]=="click": os._exit(7)
    if mode=="leader_exit" and request.get("method")=="tools/call" and request["params"]["name"]=="click":
        child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(10)"])
        record({"child_pid":child.pid}); os._exit(0)
    if request["method"]=="initialize":
        result={"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"fake","version":"1"},"instructions":"bounded"}
        if mode=="bad_initialize_response": result.pop("serverInfo")
    elif request["method"]=="tools/list":
        result={"tools":[{"name":n,"inputSchema":{"type":"object","properties":{p:{} for p in ps},"required":list(required[n]),"additionalProperties":additional.get(n,False)}} for n,ps in schemas.items()]}
        if mode=="bad_tool_schema": result["tools"][3]["inputSchema"]["properties"]["unexpected"]={}
    else:
        name=request["params"]["name"]; args=request["params"]["arguments"]
        if name=="list_windows":
            windows=[{"app_name":"Fixture","title":"Relay Desktop Fixture","pid":123,"window_id":456}]
            if mode=="delayed_window" and calls==0: windows=[]
            if mode=="ambiguous": windows*=2
            if mode=="changed" and calls>=1: windows=[{"app_name":"Other","title":"Other","pid":1,"window_id":2}]
            calls+=1; structured={"windows":windows}; error=False
        elif name=="get_window_state":
            structured={"elements":[
              {"role":"entry","label":"Name","value":"","element_token":"driver-field","frame":{"x":1}},
              {"role":"button","label":"Apply","element_token":"driver-button","index":7},
              {"role":"button","label":"Grant Camera Permission","element_token":"driver-permission"},
              {"role":"password","label":"Password","value":"secret","element_token":"driver-secret"}]}; error=False
            if mode=="deep_controls":
                structured["elements"]=[
                  {"role":"heading","label":f"Section {index}","element_token":f"driver-heading-{index}"}
                  for index in range(20)
                ]+structured["elements"]
            if mode=="browser_chrome_controls":
                structured["elements"]=[
                  {"role":"button","label":f"Chrome {index}","element_token":f"driver-chrome-{index}","element_index":index,"parent_index":None,"depth":1}
                  for index in range(20)
                ]+[
                  {"role":"document web","label":"Fixture","element_token":"driver-document","element_index":20,"parent_index":None,"depth":1},
                  {"role":"entry","label":"Name","value":"","element_token":"driver-field","element_index":21,"parent_index":20,"depth":2},
                  {"role":"button","label":"Apply","element_token":"driver-button","element_index":22,"parent_index":20,"depth":2},
                ]
            if mode=="implicit_risky_control":
                structured["elements"].append(
                  {"role":"button","label":"Dormant Override","element_token":"driver-override"}
                )
            if mode=="unlabeled_field": structured["elements"][0].pop("label")
            if mode=="disabled": structured["elements"][1]["enabled"]=False
            if mode=="bad_enabled": structured["elements"][1]["enabled"]="false"
        elif name=="type_text" and mode=="fallback" and "delivery_mode" not in args:
            structured={"code":"background_unavailable","raw":"SECRET_DRIVER_ERROR"}; error=True
        elif name=="click" and mode=="raw_error": structured={"code":"private","message":"SECRET_DRIVER_ERROR"}; error=True
        elif name=="click" and mode=="unverified": structured={"verified":False,"effect":"suspected_noop"}; error=False
        elif name=="click" and mode=="click_scalar": structured=7; error=False
        else: structured={"ok":True}; error=False
        result={"structuredContent":structured,"isError":error}
    if mode=="oversized" and request["method"]=="initialize": sys.stdout.write("x"*300000+"\n"); sys.stdout.flush(); continue
    if mode=="malformed" and request["method"]=="initialize": sys.stdout.write("not json\n"); sys.stdout.flush(); continue
    print(json.dumps({"jsonrpc":"2.0","id":ident,"result":result}),flush=True)
"""


def fake_driver(tmp_path: Path) -> tuple[Path, Path]:
    path, log = tmp_path / "cua-driver", tmp_path / "calls.jsonl"
    path.write_text(FAKE)
    path.chmod(0o755)
    return path, log


def message(tool: str, **values: str):
    base = {"version": 1, "type": "invoke", "request_id": "r", "tool": tool} | values
    return {
        "computer.capture": ComputerCaptureInvoke,
        "computer.click": ComputerClickInvoke,
        "computer.type": ComputerTypeInvoke,
    }[tool].model_validate(base)


def configured(
    path: Path, log: Path, mode: str = "normal", **kwargs: object
) -> ComputerCapability:
    env = {
        "PATH": os.environ["PATH"],
        "DISPLAY": ":99",
        "HOME": str(path.parent),
        "FAKE_MODE": mode,
        "FAKE_LOG": str(log),
        "AGENT_RELAY_AGENT_TOKEN": "SECRET",
        "HTTPS_PROXY": "http://secret-proxy",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/safe",
    }
    # Test controls are intentionally compiled into the fake: they are not allowlisted.
    text = (
        path.read_text()
        .replace('mode=os.environ.get("FAKE_MODE", "normal")', f'mode="{mode}"')
        .replace('log=os.environ.get("FAKE_LOG")', f"log={str(log)!r}")
    )
    path.write_text(text)
    path.chmod(0o755)
    return ComputerCapability(
        path,
        "Fixture",
        "Relay Desktop Fixture",
        environ=env,
        startup_timeout_seconds=2,
        action_timeout_seconds=0.15,
        shutdown_timeout_seconds=0.15,
        **kwargs,
    )


def test_computer_capability_exposes_only_constrained_tools() -> None:
    assert ComputerCapability.tools == frozenset(
        {"computer.capture", "computer.click", "computer.type"}
    )


def test_driver_path_and_environment_are_safe(tmp_path: Path) -> None:
    path, _ = fake_driver(tmp_path)
    assert validate_driver_executable(path) == path
    link = tmp_path / "link"
    link.symlink_to(path)
    for invalid in (Path("relative"), link, tmp_path):
        with pytest.raises(ValueError):
            validate_driver_executable(invalid)
    env = safe_driver_environment(
        {
            "DISPLAY": ":1",
            "PATH": "/bin",
            "XDG_RUNTIME_DIR": "/run/private",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/private/bus",
            "NO_AT_BRIDGE": "0",
            "GTK_MODULES": "gail:atk-bridge",
            "AT_SPI_BUS_TYPE": "session",
            "PSModulePath": "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\Modules",
            "AGENT_RELAY_TOKEN": "x",
            "HTTP_PROXY": "x",
            "SECRET": "x",
        }
    )
    assert env == {
        "DISPLAY": ":1",
        "PATH": "/bin",
        "XDG_RUNTIME_DIR": "/run/private",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/private/bus",
        "NO_AT_BRIDGE": "0",
        "GTK_MODULES": "gail:atk-bridge",
        "AT_SPI_BUS_TYPE": "session",
        "PSModulePath": "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\Modules",
        "CUA_DRIVER_TELEMETRY": "0",
        "CUA_DRIVER_RS_TELEMETRY_ENABLED": "0",
    }


def test_start_capture_actions_and_foreground_fallback_are_narrow(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path, log = fake_driver(tmp_path)
        capability = configured(path, log, "fallback")
        await capability.start()
        capture = await capability.invoke(message("computer.capture"))
        assert set(capture) == {"app", "window_title", "generation", "elements"}
        assert len(capture["elements"]) == 2
        assert all(
            set(item) <= {"element_id", "role", "name", "value", "enabled"}
            for item in capture["elements"]
        )
        assert "driver-" not in json.dumps(capture) and "Password" not in json.dumps(
            capture
        )
        field = next(item for item in capture["elements"] if item["role"] == "entry")[
            "element_id"
        ]
        button = next(item for item in capture["elements"] if item["role"] == "button")[
            "element_id"
        ]
        await capability.invoke(message("computer.click", element_id=field))
        typed = await capability.invoke(
            message("computer.type", element_id=field, text="hello")
        )
        assert typed == {
            "success": True,
            "generation": capture["generation"],
            "element_id": field,
        }
        await capability.invoke(message("computer.click", element_id=button))
        await capability.aclose()
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        initialize = next(item for item in calls if item.get("method") == "initialize")
        assert initialize["params"]["protocolVersion"] == "2025-06-18"
        tool_calls = [item for item in calls if item.get("method") == "tools/call"]
        capture_args = [
            item["params"]["arguments"]
            for item in tool_calls
            if item["params"]["name"] == "get_window_state"
        ]
        assert capture_args
        assert all(
            args["include_screenshot"] is False and args["max_elements"] == 300
            for args in capture_args
        )
        type_args = [
            item["params"]["arguments"]
            for item in tool_calls
            if item["params"]["name"] == "type_text"
        ]
        assert (
            len(type_args) == 2
            and "delivery_mode" not in type_args[0]
            and type_args[1]["delivery_mode"] == "foreground"
        )
        assert all(
            args["pid"] == 123
            and args["window_id"] == 456
            and args["element_token"] == "driver-field"
            for args in type_args
        )
        assert any(
            item.get("params", {}).get("name") == "end_session" for item in calls
        )
        telemetry = [item for item in calls if "argv" in item]
        mcp_processes = [item for item in calls if "mcp_argv" in item]
        assert [item["mcp_argv"] for item in mcp_processes] == [
            ["mcp", "--no-overlay"]
        ]
        assert [item["argv"] for item in telemetry] == [
            ["telemetry", "disable"],
            ["telemetry", "reset-id"],
            ["telemetry", "status", "--json"],
        ]
        assert all(
            "AGENT_RELAY_AGENT_TOKEN" not in item["env"]
            and "HTTPS_PROXY" not in item["env"]
            and item["env"].get("CUA_DRIVER_TELEMETRY") == "0"
            and item["env"].get("CUA_DRIVER_RS_TELEMETRY_ENABLED") == "0"
            for item in telemetry + mcp_processes
        )

    asyncio.run(scenario())


def test_start_retries_bounded_window_discovery(tmp_path: Path) -> None:
    async def scenario() -> None:
        path, log = fake_driver(tmp_path)
        capability = configured(path, log, "delayed_window")
        await capability.start()
        await capability.aclose()
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        assert sum(
            item.get("params", {}).get("name") == "list_windows" for item in calls
        ) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("mode", "expected_phase"),
    [
        ("wrong_id", "initialize"),
        ("malformed", "initialize"),
        ("oversized", "initialize"),
        ("bad_initialize_response", "initialize-response"),
        ("bad_tool_schema", "tools-list"),
        ("telemetry_oversized", "privacy-status"),
        ("telemetry_invalid_json", "privacy-status-json"),
        ("telemetry_on", "privacy-status-values"),
        ("ambiguous", "window-select"),
    ],
)
def test_startup_failures_are_closed_and_bounded(
    tmp_path: Path, mode: str, expected_phase: str
) -> None:
    async def scenario() -> None:
        path, log = fake_driver(tmp_path)
        capability = configured(path, log, mode)
        with pytest.raises(ComputerUnavailableError) as error:
            await capability.start()
        assert str(error.value) == "computer capability unavailable"
        assert error.value.startup_phase == expected_phase
        await asyncio.wait_for(capability.wait_unavailable(), 0.5)
        await capability.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["hang", "exit", "raw_error"])
def test_action_failure_is_bounded_unavailable_and_secret_free(
    tmp_path: Path, mode: str
) -> None:
    async def scenario() -> None:
        path, log = fake_driver(tmp_path)
        capability = configured(path, log, mode)
        await capability.start()
        capture = await capability.invoke(message("computer.capture"))
        target = capture["elements"][1]["element_id"]
        with pytest.raises(ComputerUnavailableError) as error:
            await asyncio.wait_for(
                capability.invoke(message("computer.click", element_id=target)), 0.4
            )
        assert "SECRET_DRIVER_ERROR" not in str(error.value)
        await asyncio.wait_for(capability.wait_unavailable(), 0.5)
        await capability.aclose()

    asyncio.run(scenario())


def test_allowlisted_window_change_fails_fresh_capture(tmp_path: Path) -> None:
    async def scenario() -> None:
        path, log = fake_driver(tmp_path)
        capability = configured(path, log, "changed")
        await capability.start()
        with pytest.raises(ComputerUnavailableError):
            await capability.invoke(message("computer.capture"))
        await asyncio.wait_for(capability.wait_unavailable(), 0.5)
        await capability.aclose()

    asyncio.run(scenario())


def test_unverified_successful_click_is_accepted_exactly_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        path, log = fake_driver(tmp_path)
        capability = configured(path, log, "unverified")
        await capability.start()
        capture = await capability.invoke(message("computer.capture"))
        target = capture["elements"][1]["element_id"]
        assert await capability.invoke(
            message("computer.click", element_id=target)
        ) == {
            "success": True,
            "generation": capture["generation"],
            "element_id": target,
        }
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        assert sum(item.get("params", {}).get("name") == "click" for item in calls) == 1
        await capability.aclose()

    asyncio.run(scenario())


def test_capture_prioritizes_actionable_controls_after_structural_nodes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path, log = fake_driver(tmp_path)
        capability = configured(path, log, "deep_controls")
        await capability.start()
        capture = await capability.invoke(message("computer.capture"))
        await capability.aclose()
        assert len(capture["elements"]) == 12
        names = {item["name"] for item in capture["elements"]}
        assert {"Name", "Apply"} <= names
        assert not {"Grant Camera Permission", "Password"} & names

    asyncio.run(scenario())


def test_capture_prioritizes_web_document_controls_over_browser_chrome(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path, log = fake_driver(tmp_path)
        capability = configured(path, log, "browser_chrome_controls")
        await capability.start()
        capture = await capability.invoke(message("computer.capture"))
        await capability.aclose()
        assert len(capture["elements"]) == 12
        names = {item["name"] for item in capture["elements"]}
        assert {"Name", "Apply"} <= names

    asyncio.run(scenario())


def test_capture_omits_implicit_privileged_override_control(tmp_path: Path) -> None:
    async def scenario() -> None:
        path, log = fake_driver(tmp_path)
        capability = configured(path, log, "implicit_risky_control")
        await capability.start()
        capture = await capability.invoke(message("computer.capture"))
        await capability.aclose()
        assert [item["name"] for item in capture["elements"]] == ["Name", "Apply"]

    asyncio.run(scenario())


def test_capture_keeps_unlabeled_editable_element_from_linux_driver(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path, log = fake_driver(tmp_path)
        capability = configured(path, log, "unlabeled_field")
        await capability.start()
        capture = await capability.invoke(message("computer.capture"))
        await capability.aclose()
        fields = [item for item in capture["elements"] if item["role"] == "entry"]
        assert len(fields) == 1
        assert fields[0]["name"] == ""

    asyncio.run(scenario())


def test_capture_omits_disabled_and_rejects_non_boolean_enabled(tmp_path: Path) -> None:
    async def scenario() -> None:
        path, log = fake_driver(tmp_path)
        capability = configured(path, log, "disabled")
        await capability.start()
        capture = await capability.invoke(message("computer.capture"))
        assert [item["name"] for item in capture["elements"]] == ["Name"]
        assert capture["elements"][0]["value"] == ""
        await capability.aclose()

        path, log = fake_driver(tmp_path)
        capability = configured(path, log, "bad_enabled")
        await capability.start()
        with pytest.raises(ComputerUnavailableError):
            await capability.invoke(message("computer.capture"))
        await capability.aclose()

    asyncio.run(scenario())


def test_click_rejects_malformed_non_object_driver_result(tmp_path: Path) -> None:
    async def scenario() -> None:
        path, log = fake_driver(tmp_path)
        capability = configured(path, log, "click_scalar")
        await capability.start()
        capture = await capability.invoke(message("computer.capture"))
        with pytest.raises(ComputerUnavailableError):
            await capability.invoke(message("computer.click", element_id=capture["elements"][1]["element_id"]))
        await capability.aclose()

    asyncio.run(scenario())


def test_leader_exit_still_terminates_process_group_descendant(tmp_path: Path) -> None:
    async def scenario() -> None:
        path, log = fake_driver(tmp_path)
        capability = configured(path, log, "leader_exit")
        await capability.start()
        capture = await capability.invoke(message("computer.capture"))
        target = capture["elements"][1]["element_id"]
        with pytest.raises(ComputerUnavailableError):
            await capability.invoke(message("computer.click", element_id=target))
        await asyncio.wait_for(capability.wait_unavailable(), 0.5)
        child_pid = next(
            item["child_pid"]
            for item in map(json.loads, log.read_text().splitlines())
            if "child_pid" in item
        )
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        await capability.aclose()

    asyncio.run(scenario())


def test_oversized_request_does_not_leak_pending_entry(tmp_path: Path) -> None:
    async def scenario() -> None:
        path, log = fake_driver(tmp_path)
        capability = configured(path, log)
        await capability.start()
        with pytest.raises(ValueError):
            await capability._request(
                "oversized", {"value": "x" * 300000}, capability._action_timeout
            )
        assert capability._pending == {}
        assert (await capability.invoke(message("computer.capture")))["elements"]
        await capability.aclose()

    asyncio.run(scenario())


def test_concurrent_reset_is_idempotent(tmp_path: Path) -> None:
    async def scenario() -> None:
        path, log = fake_driver(tmp_path)
        capability = configured(path, log)
        await capability.start()
        process = capability._process
        assert process is not None
        await asyncio.gather(
            capability._reset(), capability._reset(), capability._reset()
        )
        assert capability._process is None
        assert capability._reader_task is None
        assert capability._exit_task is None
        assert capability._pending == {}
        await asyncio.wait_for(capability.wait_unavailable(), 0.5)
        with pytest.raises(ProcessLookupError):
            os.kill(process.pid, 0)

    asyncio.run(scenario())


def test_stale_duplicate_and_semantic_actions_are_local(tmp_path: Path) -> None:
    async def scenario() -> None:
        path, log = fake_driver(tmp_path)
        capability = configured(path, log)
        await capability.start()
        first = await capability.invoke(message("computer.capture"))
        field, button = (
            first["elements"][0]["element_id"],
            first["elements"][1]["element_id"],
        )
        before = log.read_text().count('"name":"type_text"')
        with pytest.raises(ComputerUnavailableError):
            await capability.invoke(
                message("computer.type", element_id=field, text="before")
            )
        assert log.read_text().count('"name":"type_text"') == before
        await capability.invoke(message("computer.click", element_id=button))
        before = log.read_text().count('"name":"click"')
        with pytest.raises(ComputerUnavailableError):
            await capability.invoke(message("computer.click", element_id=button))
        assert log.read_text().count('"name":"click"') == before
        fresh = await capability.invoke(message("computer.capture"))
        with pytest.raises(ComputerUnavailableError):
            await capability.invoke(
                message("computer.click", element_id=first["elements"][1]["element_id"])
            )
        assert fresh["generation"] != first["generation"]
        await capability.aclose()

    asyncio.run(scenario())


def test_cancellation_terminates_backend_process_group(tmp_path: Path) -> None:
    async def scenario() -> None:
        path, log = fake_driver(tmp_path)
        capability = configured(path, log, "hang")
        await capability.start()
        capture = await capability.invoke(message("computer.capture"))
        task = asyncio.create_task(
            capability.invoke(
                message(
                    "computer.click", element_id=capture["elements"][1]["element_id"]
                )
            )
        )
        pid = capability._process.pid
        await asyncio.sleep(0.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(capability.wait_unavailable(), 0.5)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        child_pid = next(
            item["child_pid"]
            for item in map(json.loads, log.read_text().splitlines())
            if "child_pid" in item
        )
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        await capability.aclose()

    asyncio.run(scenario())

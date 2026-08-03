from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agent_relay.protocol import (
    MAX_BROWSER_ELEMENT_ID_LENGTH,
    MAX_BROWSER_FILL_VALUE_LENGTH,
    MAX_BROWSER_URL_LENGTH,
    MAX_COMPUTER_ELEMENT_ID_LENGTH,
    MAX_COMPUTER_TYPE_TEXT_LENGTH,
    MAX_RESULT_JSON_BYTES,
    AgentError,
    AgentResult,
    BrowserBackInvoke,
    BrowserClickInvoke,
    BrowserFillInvoke,
    BrowserListTabsInvoke,
    BrowserNavigateInvoke,
    BrowserScrollInvoke,
    BrowserSnapshotInvoke,
    BrowserTypeInvoke,
    Capabilities,
    ComputerCaptureInvoke,
    ComputerClickInvoke,
    ComputerTypeInvoke,
    InvokeMessage,
    Progress,
    Register,
    SystemPingInvoke,
    TerminalExecInvoke,
    parse_agent_message,
    parse_server_message,
)


def test_v2_generic_invoke_and_provider_result_are_the_only_application_frames() -> None:
    invoke = parse_server_message(
        {
            "version": 2,
            "type": "invoke",
            "request_id": "r-1",
            "tool_name": "cua.get_accessibility_tree",
            "arguments": {},
        }
    )
    assert isinstance(invoke, InvokeMessage)
    assert invoke.tool_name == "cua.get_accessibility_tree"
    assert invoke.arguments == {}

    result = parse_agent_message(
        {
            "version": 2,
            "type": "result",
            "request_id": "r-1",
            "result": {
                "content": [
                    {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}
                ],
                "structuredContent": {"ok": True},
                "isError": False,
            },
        }
    )
    assert isinstance(result, AgentResult)
    assert result.result.structured_content == {"ok": True}


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 1, "type": "invoke", "request_id": "r", "tool": "system.ping"},
        {"version": 2, "type": "invoke", "request_id": "r", "arguments": {}},
        {"version": 2, "type": "invoke", "request_id": "r", "tool_name": "bad/name", "arguments": {}},
        {"version": 2, "type": "invoke", "request_id": "r", "tool_name": "x.y", "arguments": []},
        {"version": 2, "type": "invoke", "request_id": "r", "tool_name": "x.y", "arguments": {}, "handler": "x"},
    ],
)
def test_v2_generic_invoke_rejects_legacy_malformed_and_executable_fields(
    payload: dict[str, object],
) -> None:
    with pytest.raises((ValidationError, ValueError)):
        parse_server_message(payload)


def test_parses_strict_versioned_token_free_register() -> None:
    message = parse_agent_message(
        {"version": 1, "type": "register", "device_id": "device-a"}
    )

    assert isinstance(message, Register)
    assert message.model_dump(mode="json") == {
        "version": 1,
        "type": "register",
        "device_id": "device-a",
    }
    assert "token" not in repr(message)
    assert "secret" not in str(message)


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 2, "type": "register", "device_id": "device-a", "token": "x"},
        {"version": 1, "type": "unknown"},
        {
            "version": 1,
            "type": "register",
            "device_id": "device-a",
            "token": "x",
            "extra": 1,
        },
    ],
)
def test_rejects_bad_version_type_and_extra_fields(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        parse_agent_message(payload)


def test_terminal_exec_only_allows_closed_command_ids() -> None:
    invoke = TerminalExecInvoke(
        version=1,
        type="invoke",
        request_id="req-1",
        tool="terminal.exec",
        command_id="pwd",
    )
    assert invoke.command_id == "pwd"

    with pytest.raises(ValidationError):
        TerminalExecInvoke(
            version=1,
            type="invoke",
            request_id="req-1",
            tool="terminal.exec",
            command_id="rm_rf",
        )


def test_invoke_message_is_an_explicit_closed_union() -> None:
    assert InvokeMessage.model_fields.keys() == {
        "version", "type", "request_id", "tool_name", "arguments"
    }


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        (
            {
                "version": 1,
                "type": "invoke",
                "request_id": "ping-1",
                "tool": "system.ping",
            },
            SystemPingInvoke,
        ),
        (
            {
                "version": 1,
                "type": "invoke",
                "request_id": "exec-1",
                "tool": "terminal.exec",
                "command_id": "pwd",
            },
            TerminalExecInvoke,
        ),
        (
            {
                "version": 1,
                "type": "invoke",
                "request_id": "tabs-1",
                "tool": "browser.list_tabs",
            },
            BrowserListTabsInvoke,
        ),
        (
            {
                "version": 1,
                "type": "invoke",
                "request_id": "nav-1",
                "tool": "browser.navigate",
                "url": "https://example.test/",
            },
            BrowserNavigateInvoke,
        ),
        (
            {
                "version": 1,
                "type": "invoke",
                "request_id": "read-1",
                "tool": "browser.snapshot",
            },
            BrowserSnapshotInvoke,
        ),
        (
            {
                "version": 1,
                "type": "invoke",
                "request_id": "fill-1",
                "tool": "browser.fill",
                "element_id": "field-1",
                "value": "hello",
            },
            BrowserFillInvoke,
        ),
        (
            {
                "version": 1,
                "type": "invoke",
                "request_id": "click-1",
                "tool": "browser.click",
                "element_id": "button-1",
            },
            BrowserClickInvoke,
        ),
        (
            {
                "version": 1,
                "type": "invoke",
                "request_id": "scroll-1",
                "tool": "browser.scroll",
                "direction": "down",
            },
            BrowserScrollInvoke,
        ),
        (
            {
                "version": 1,
                "type": "invoke",
                "request_id": "type-browser-1",
                "tool": "browser.type",
                "element_id": "field-1",
                "text": "hello",
            },
            BrowserTypeInvoke,
        ),
        (
            {
                "version": 1,
                "type": "invoke",
                "request_id": "back-1",
                "tool": "browser.back",
            },
            BrowserBackInvoke,
        ),
        (
            {
                "version": 1,
                "type": "invoke",
                "request_id": "capture-1",
                "tool": "computer.capture",
            },
            ComputerCaptureInvoke,
        ),
        (
            {
                "version": 1,
                "type": "invoke",
                "request_id": "computer-click-1",
                "tool": "computer.click",
                "element_id": "opaque-generation-element",
            },
            ComputerClickInvoke,
        ),
        (
            {
                "version": 1,
                "type": "invoke",
                "request_id": "type-1",
                "tool": "computer.type",
                "element_id": "opaque-generation-element",
                "text": "hello",
            },
            ComputerTypeInvoke,
        ),
    ],
)
def test_invoke_parsing_is_discriminated_by_type_and_tool(
    payload: dict[str, object], expected_type: type[object]
) -> None:
    del expected_type
    with pytest.raises((ValidationError, ValueError)):
        parse_server_message(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "version": 1,
            "type": "invoke",
            "request_id": "ping-1",
            "tool": "system.ping",
            "command_id": "pwd",
        },
        {
            "version": 1,
            "type": "invoke",
            "request_id": "exec-1",
            "tool": "terminal.exec",
        },
        {
            "version": 1,
            "type": "invoke",
            "request_id": "exec-1",
            "tool": "terminal.exec",
            "command_id": "arbitrary",
        },
        {
            "version": 1,
            "type": "invoke",
            "request_id": "other-1",
            "tool": "arbitrary",
            "args": {},
        },
    ],
)
def test_invoke_parsing_forbids_unknown_and_wrong_tool_fields(
    payload: dict[str, object],
) -> None:
    with pytest.raises((ValidationError, ValueError)):
        parse_server_message(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "version": 1,
            "type": "invoke",
            "request_id": "x",
            "tool": "browser.list_tabs",
            "url": "https://example.test",
        },
        {
            "version": 1,
            "type": "invoke",
            "request_id": "x",
            "tool": "browser.snapshot",
            "element_id": "x",
        },
        {"version": 1, "type": "invoke", "request_id": "x", "tool": "browser.navigate"},
        {
            "version": 1,
            "type": "invoke",
            "request_id": "x",
            "tool": "browser.fill",
            "element_id": "x",
        },
        {
            "version": 1,
            "type": "invoke",
            "request_id": "x",
            "tool": "browser.click",
            "element_id": 1,
        },
    ],
)
def test_browser_invoke_parsing_fails_closed(payload: dict[str, object]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        parse_server_message(payload)


@pytest.mark.parametrize(
    ("model", "field", "limit"),
    [
        (BrowserNavigateInvoke, "url", MAX_BROWSER_URL_LENGTH),
        (BrowserClickInvoke, "element_id", MAX_BROWSER_ELEMENT_ID_LENGTH),
        (BrowserFillInvoke, "value", MAX_BROWSER_FILL_VALUE_LENGTH),
    ],
)
def test_browser_invoke_strings_are_nonempty_and_bounded(
    model: type[object], field: str, limit: int
) -> None:
    base = {"version": 1, "type": "invoke", "request_id": "x"}
    payloads = {
        BrowserNavigateInvoke: {"tool": "browser.navigate", "url": "x"},
        BrowserClickInvoke: {"tool": "browser.click", "element_id": "x"},
        BrowserFillInvoke: {"tool": "browser.fill", "element_id": "x", "value": "x"},
    }
    valid = base | payloads[model]  # type: ignore[index]
    model.model_validate(valid | {field: "x" * limit})  # type: ignore[attr-defined]
    for rejected in ("", "x" * (limit + 1)):
        with pytest.raises(ValidationError):
            model.model_validate(valid | {field: rejected})  # type: ignore[attr-defined]


def test_computer_invokes_are_closed_bounded_and_preserve_semantic_target() -> None:
    base = {"version": 1, "type": "invoke", "request_id": "x"}
    ComputerCaptureInvoke.model_validate(base | {"tool": "computer.capture"})
    ComputerClickInvoke.model_validate(
        base
        | {
            "tool": "computer.click",
            "element_id": "x" * MAX_COMPUTER_ELEMENT_ID_LENGTH,
        }
    )
    ComputerTypeInvoke.model_validate(
        base
        | {
            "tool": "computer.type",
            "element_id": "opaque-id",
            "text": "x" * MAX_COMPUTER_TYPE_TEXT_LENGTH,
        }
    )
    with pytest.raises((ValidationError, ValueError)):
        parse_server_message(
            base
            | {
                "tool": "computer.type",
                "element_id": "opaque-id",
                "text": "launch \U0001f680",
            }
        )

    rejected = [
        {"tool": "computer.capture", "coordinates": [1, 2]},
        {"tool": "computer.click", "element_id": "", "x": 1},
        {"tool": "computer.type", "text": "missing target"},
        {"tool": "computer.type", "element_id": "opaque-id", "text": ""},
        {"tool": "computer.type", "element_id": "opaque-id", "text": "a\nb"},
        {"tool": "computer.type", "element_id": "opaque-id", "text": "a\x00b"},
        {"tool": "computer.type", "element_id": "opaque-id", "text": "a\u0085b"},
        {"tool": "computer.type", "element_id": "opaque-id", "text": "a\u202eb"},
        {
            "tool": "computer.type",
            "element_id": "opaque-id",
            "text": "x" * (MAX_COMPUTER_TYPE_TEXT_LENGTH + 1),
        },
    ]
    for payload in rejected:
        with pytest.raises((ValidationError, ValueError)):
            parse_server_message(base | payload)


def test_register_frame_has_no_credential_field_or_secret_repr() -> None:
    message = Register(version=1, type="register", device_id="device-a")
    assert message.model_dump(mode="json") == {
        "version": 1,
        "type": "register",
        "device_id": "device-a",
    }
    assert "secret" not in repr(message)
    assert "token" not in json.dumps(message.model_dump(mode="json"))


def test_agent_result_carries_bounded_provider_result() -> None:
    result = AgentResult(
        version=2,
        type="result",
        request_id="request",
        result={
            "content": [{"type": "text", "text": "ok"}],
            "structuredContent": {"command_id": "pwd"},
        },
    )
    assert result.result.structured_content == {"command_id": "pwd"}


def test_protocol_rejects_register_frame_credentials_and_bounds_agent_payloads() -> None:
    with pytest.raises(ValidationError):
        Register(
            version=1,
            type="register",
            device_id="device-a",
            token="secret",  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        Capabilities(version=1, type="capabilities", tools=["system.ping"] * 17)
    with pytest.raises(ValidationError):
        AgentResult(
            version=2,
            type="result",
            request_id="request",
            result={"content": [{"type": "text", "text": "x" * MAX_RESULT_JSON_BYTES}]},
        )
    deeply_nested: dict[str, object] = {}
    cursor = deeply_nested
    for _ in range(17):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child
    with pytest.raises(ValidationError):
        AgentResult(
            version=2,
            type="result",
            request_id="request",
            result={"content": [], "structuredContent": deeply_nested},
        )
    with pytest.raises(ValidationError):
        AgentError(
            version=2,
            type="error",
            request_id="request",
            error={"code": "failed", "message": "x" * 513},
        )
    with pytest.raises(ValidationError):
        Progress(
            version=2,
            type="progress",
            request_id="request",
            progress=1,
            message="x" * 513,
        )

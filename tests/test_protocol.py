from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

import agent_relay.protocol as protocol
from agent_relay.protocol import (
    MAX_CAPABILITIES,
    MAX_RESULT_JSON_BYTES,
    AgentError,
    AgentResult,
    Capabilities,
    InvokeMessage,
    Progress,
    Register,
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


def test_protocol_does_not_expose_operation_specific_invoke_models() -> None:
    legacy_names = (
        "SystemPingInvoke",
        "TerminalExecInvoke",
        "BrowserListTabsInvoke",
        "BrowserNavigateInvoke",
        "BrowserSnapshotInvoke",
        "BrowserFillInvoke",
        "BrowserClickInvoke",
        "BrowserScrollInvoke",
        "BrowserTypeInvoke",
        "BrowserBackInvoke",
        "ComputerCaptureInvoke",
        "ComputerClickInvoke",
        "ComputerTypeInvoke",
    )
    assert all(not hasattr(protocol, name) for name in legacy_names)


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


def test_generic_invoke_preserves_provider_owned_arguments() -> None:
    invoke = InvokeMessage(
        version=2,
        type="invoke",
        request_id="req-1",
        tool_name="terminal.exec",
        arguments={"command_id": "pwd"},
    )
    assert invoke.tool_name == "terminal.exec"
    assert invoke.arguments == {"command_id": "pwd"}

    unvalidated = InvokeMessage(
        version=2,
        type="invoke",
        request_id="req-2",
        tool_name="terminal.exec",
        arguments={"command_id": "provider-owned-command"},
    )
    assert unvalidated.arguments == {"command_id": "provider-owned-command"}


def test_invoke_message_is_an_explicit_closed_union() -> None:
    assert InvokeMessage.model_fields.keys() == {
        "version", "type", "request_id", "tool_name", "arguments"
    }


@pytest.mark.parametrize(
    "payload",
    [
        {
            "version": 2,
            "type": "invoke",
            "request_id": "ping-1",
            "tool_name": "system.ping",
            "arguments": {},
        },
        {
            "version": 2,
            "type": "invoke",
            "request_id": "exec-1",
            "tool_name": "terminal.exec",
            "arguments": {"command_id": "pwd"},
        },
        {
            "version": 2,
            "type": "invoke",
            "request_id": "browser-1",
            "tool_name": "browser.click",
            "arguments": {"element_id": "provider-owned-id"},
        },
        {
            "version": 2,
            "type": "invoke",
            "request_id": "computer-1",
            "tool_name": "computer.type",
            "arguments": {"element_id": "provider-owned-id", "text": "hello"},
        },
    ],
)
def test_generic_invoke_parsing_accepts_provider_tool_arguments(
    payload: dict[str, object],
) -> None:
    message = parse_server_message(payload)
    assert isinstance(message, InvokeMessage)
    assert message.tool_name


@pytest.mark.parametrize(
    "payload",
    [
        {
            "version": 1,
            "type": "invoke",
            "request_id": "legacy-1",
            "tool": "system.ping",
        },
        {
            "version": 2,
            "type": "invoke",
            "request_id": "missing-name",
            "arguments": {},
        },
        {
            "version": 2,
            "type": "invoke",
            "request_id": "legacy-field",
            "tool": "system.ping",
            "arguments": {},
        },
        {
            "version": 2,
            "type": "invoke",
            "request_id": "array-args",
            "tool_name": "system.ping",
            "arguments": [],
        },
        {
            "version": 2,
            "type": "invoke",
            "request_id": "handler-field",
            "tool_name": "system.ping",
            "arguments": {},
            "handler": "not-accepted",
        },
        {
            "version": 2,
            "type": "invoke",
            "request_id": "bad-name",
            "tool_name": "bad/name",
            "arguments": {},
        },
    ],
)
def test_generic_invoke_rejects_legacy_and_malformed_frames(
    payload: dict[str, object],
) -> None:
    with pytest.raises((ValidationError, ValueError)):
        parse_server_message(payload)


def test_generic_invoke_keeps_provider_arguments_bounded_but_opaque() -> None:
    message = InvokeMessage(
        version=2,
        type="invoke",
        request_id="opaque-1",
        tool_name="cua.provider_tool",
        arguments={
            "provider_owned": {"value": "kept"},
            "items": [1, True, None],
        },
    )
    assert message.arguments == {
        "provider_owned": {"value": "kept"},
        "items": [1, True, None],
    }

    with pytest.raises(ValidationError):
        InvokeMessage(
            version=2,
            type="invoke",
            request_id="opaque-2",
            tool_name="cua.provider_tool",
            arguments=[],  # type: ignore[arg-type]
        )

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
        Capabilities(
            version=1,
            type="capabilities",
            tools=["system.ping"] * (MAX_CAPABILITIES + 1),
        )
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

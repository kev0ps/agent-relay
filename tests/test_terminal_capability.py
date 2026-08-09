from __future__ import annotations

import asyncio

import pytest

from agent_relay.capabilities.base import CapabilityProviderClient
from agent_relay.capabilities.system import SystemCapability
from agent_relay.capabilities.terminal import TerminalCapability
from agent_relay.runner import CommandResult


class _Runner:
    def __init__(self, result: CommandResult | None = None) -> None:
        self.calls: list[str] = []
        self.result = result or CommandResult(stdout="/workspace\n", exit_code=0)

    async def run(self, command_id: str) -> CommandResult:
        self.calls.append(command_id)
        return self.result


def test_terminal_exposes_the_fixed_provider_descriptor_inventory() -> None:
    from agent_relay.capabilities import terminal

    descriptors = terminal.TERMINAL_PROVIDER_DESCRIPTORS
    assert [descriptor.provider_name for descriptor in descriptors] == ["terminal"]
    assert [descriptor.tool_name for descriptor in descriptors] == ["exec"]
    assert descriptors[0].input_schema["properties"]["command_id"]["enum"] == [
        "pwd",
        "whoami",
        "python_version",
        "git_status",
        "git_branch",
    ]


def test_terminal_dispatches_through_generic_provider_client() -> None:
    runner = _Runner()
    capability = TerminalCapability(runner)
    client = CapabilityProviderClient(
        capability, capability.TERMINAL_PROVIDER_DESCRIPTORS
    )

    result = asyncio.run(client.call_tool("exec", {"command_id": "pwd"}))

    assert runner.calls == ["pwd"]
    assert result.structured_content == {
        "command_id": "pwd",
        "stdout": "/workspace\n",
        "stderr": "",
        "exit_code": 0,
        "timed_out": False,
        "stdout_truncated": False,
        "stderr_truncated": False,
    }


def test_terminal_rejects_shell_text_and_extra_arguments() -> None:
    runner = _Runner()
    client = CapabilityProviderClient(
        TerminalCapability(runner),
        TerminalCapability.TERMINAL_PROVIDER_DESCRIPTORS,
    )

    with pytest.raises(ValueError):
        asyncio.run(client.call_tool("exec", {"command_id": "pwd && whoami"}))
    with pytest.raises(ValueError):
        asyncio.run(client.call_tool("exec", {"command_id": "pwd", "args": []}))
    assert runner.calls == []


def test_system_exposes_a_separate_generic_provider_descriptor() -> None:
    from agent_relay.capabilities import system

    descriptor = system.SYSTEM_PROVIDER_DESCRIPTORS[0]
    assert descriptor.provider_name == "system"
    assert descriptor.tool_name == "ping"
    assert descriptor.input_schema == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


def test_system_ping_uses_the_generic_provider_adapter() -> None:
    capability = SystemCapability()
    client = CapabilityProviderClient(
        capability, capability.SYSTEM_PROVIDER_DESCRIPTORS
    )

    result = asyncio.run(client.call_tool("ping", {}))

    assert result.structured_content == {"pong": True}

from __future__ import annotations

import asyncio

import pytest

from agent_relay.capabilities.base import LocalCapability
from agent_relay.capabilities.system import SystemCapability
from agent_relay.capabilities.terminal import TerminalCapability
from agent_relay.protocol import InvokeMessage
from agent_relay.runner import CommandResult


def test_local_capability_protocol_is_typed() -> None:
    capability: LocalCapability = SystemCapability()
    assert capability.tools == frozenset({"system.ping"})


def test_system_capability_accepts_only_system_ping() -> None:
    async def scenario() -> None:
        capability = SystemCapability()
        assert await capability.invoke(
            InvokeMessage(
                version=2,
                type="invoke",
                request_id="ping",
                tool_name="system.ping",
                arguments={},
            )
        ) == {"pong": True}
        with pytest.raises(ValueError, match="unsupported invocation"):
            await capability.invoke(
                InvokeMessage(
                    version=2,
                    type="invoke",
                    request_id="exec",
                    tool_name="terminal.exec",
                    arguments={"command_id": "pwd"},
                )
            )
        await capability.aclose()

    asyncio.run(scenario())


def test_terminal_capability_accepts_only_terminal_exec() -> None:
    class Runner:
        calls: list[str] = []

        async def run(self, command_id: str) -> CommandResult:
            self.calls.append(command_id)
            return CommandResult(stdout="/workspace\n", exit_code=0)

    async def scenario() -> None:
        runner = Runner()
        capability = TerminalCapability(runner)
        assert await capability.invoke(
            InvokeMessage(
                version=2,
                type="invoke",
                request_id="exec",
                tool_name="terminal.exec",
                arguments={"command_id": "pwd"},
            )
        ) == {
            "command_id": "pwd",
            "stdout": "/workspace\n",
            "stderr": "",
            "exit_code": 0,
            "timed_out": False,
            "stdout_truncated": False,
            "stderr_truncated": False,
        }
        assert runner.calls == ["pwd"]
        with pytest.raises(ValueError, match="unsupported invocation"):
            await capability.invoke(
                InvokeMessage(
                    version=2,
                    type="invoke",
                    request_id="ping",
                    tool_name="system.ping",
                    arguments={},
                )
            )
        await capability.aclose()

    asyncio.run(scenario())


def test_terminal_capability_preserves_safe_command_failure() -> None:
    class Runner:
        async def run(self, command_id: str) -> CommandResult:
            return CommandResult(error="sensitive runner detail")

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="configured command failed"):
            await TerminalCapability(Runner()).invoke(
                InvokeMessage(
                    version=2,
                    type="invoke",
                    request_id="exec",
                    tool_name="terminal.exec",
                    arguments={"command_id": "pwd"},
                )
            )

    asyncio.run(scenario())

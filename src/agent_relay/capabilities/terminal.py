"""Local terminal capability backed by the fixed command runner."""

from __future__ import annotations

from typing import Protocol

from ..protocol import TerminalExecInvoke
from ..runner import CommandResult
from .base import CommandFailedError, InvokeMessage


class CommandRunnerProtocol(Protocol):
    async def run(self, command_id: str) -> CommandResult: ...


class TerminalCapability:
    tools = frozenset({"terminal.exec"})

    def __init__(self, runner: CommandRunnerProtocol) -> None:
        self._runner = runner

    async def start(self) -> None:
        return None

    async def wait_unavailable(self) -> None:
        import asyncio
        await asyncio.Future()

    async def invoke(self, message: InvokeMessage) -> dict[str, object]:
        if not isinstance(message, TerminalExecInvoke):
            raise ValueError("unsupported invocation")
        command = await self._runner.run(message.command_id)
        if command.error is not None:
            raise CommandFailedError()
        return _terminal_result(message.command_id, command)

    async def aclose(self) -> None:
        return None


def _terminal_result(command_id: str, result: CommandResult) -> dict[str, object]:
    return {
        "command_id": command_id,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
    }

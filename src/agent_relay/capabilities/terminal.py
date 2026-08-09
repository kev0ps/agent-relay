"""Local terminal capability backed by the fixed command runner."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from ..protocol import CommandId
from ..provider_tools import ProviderToolDescriptor
from ..runner import CommandResult
from .base import CommandFailedError, InvokeMessage


class CommandRunnerProtocol(Protocol):
    async def run(self, command_id: str) -> CommandResult: ...


class _TerminalArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command_id: CommandId


TERMINAL_COMMAND_IDS: tuple[str, ...] = (
    "pwd",
    "whoami",
    "python_version",
    "git_status",
    "git_branch",
)

TERMINAL_PROVIDER_DESCRIPTORS: tuple[ProviderToolDescriptor, ...] = (
    ProviderToolDescriptor(
        provider_name="terminal",
        tool_name="exec",
        public_name="exec",
        description="fixed allowlisted terminal command",
        input_schema={
            "type": "object",
            "properties": {
                "command_id": {
                    "type": "string",
                    "enum": list(TERMINAL_COMMAND_IDS),
                }
            },
            "required": ["command_id"],
            "additionalProperties": False,
        },
        risk="interaction",
    ),
)


class TerminalCapability:
    tools = frozenset({"terminal.exec"})
    TERMINAL_PROVIDER_DESCRIPTORS = TERMINAL_PROVIDER_DESCRIPTORS

    def __init__(self, runner: CommandRunnerProtocol) -> None:
        self._runner = runner

    async def start(self) -> None:
        return None

    async def wait_unavailable(self) -> None:
        import asyncio
        await asyncio.Future()

    async def invoke(self, message: InvokeMessage) -> dict[str, object]:
        if message.tool_name != "terminal.exec":
            raise ValueError("unsupported invocation")
        try:
            arguments = _TerminalArguments.model_validate(message.arguments)
        except ValidationError:
            raise ValueError("unsupported invocation") from None
        command = await self._runner.run(arguments.command_id)
        if command.error is not None:
            raise CommandFailedError()
        return _terminal_result(arguments.command_id, command)

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

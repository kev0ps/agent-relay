"""Internal typing shared by local Relay capabilities."""

from __future__ import annotations

from typing import Protocol

from ..protocol import InvokeMessage, ToolName

CapabilityName = ToolName


class LocalCapability(Protocol):
    tools: frozenset[ToolName]

    async def start(self) -> None: ...

    async def invoke(self, message: InvokeMessage) -> dict[str, object]: ...

    async def wait_unavailable(self) -> None: ...

    async def aclose(self) -> None: ...


class CommandFailedError(RuntimeError):
    """The fixed runner reported a failure without exposing its details."""

    def __init__(self) -> None:
        super().__init__("configured command failed")

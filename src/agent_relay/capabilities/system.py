"""Local system capability."""

from __future__ import annotations

from ..protocol import SystemPingInvoke
from .base import InvokeMessage


class SystemCapability:
    tools = frozenset({"system.ping"})

    async def start(self) -> None:
        return None

    async def wait_unavailable(self) -> None:
        import asyncio
        await asyncio.Future()

    async def invoke(self, message: InvokeMessage) -> dict[str, object]:
        if not isinstance(message, SystemPingInvoke):
            raise ValueError("unsupported invocation")
        return {"pong": True}

    async def aclose(self) -> None:
        return None

"""Local system capability."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, ValidationError

from .base import InvokeMessage


class _SystemArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SystemCapability:
    tools = frozenset({"system.ping"})

    async def start(self) -> None:
        return None

    async def wait_unavailable(self) -> None:
        import asyncio
        await asyncio.Future()

    async def invoke(self, message: InvokeMessage) -> dict[str, object]:
        if message.tool_name != "system.ping":
            raise ValueError("unsupported invocation")
        try:
            _SystemArguments.model_validate(message.arguments)
        except ValidationError:
            raise ValueError("unsupported invocation") from None
        return {"pong": True}

    async def aclose(self) -> None:
        return None

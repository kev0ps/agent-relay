"""Local system capability."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, ValidationError

from ..provider_tools import ProviderToolDescriptor
from .base import InvokeMessage


class _SystemArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


SYSTEM_PROVIDER_DESCRIPTORS: tuple[ProviderToolDescriptor, ...] = (
    ProviderToolDescriptor(
        provider_name="system",
        tool_name="ping",
        public_name="ping",
        description="fixed local health check",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        risk="read_only",
    ),
)


class SystemCapability:
    tools = frozenset({"system.ping"})
    SYSTEM_PROVIDER_DESCRIPTORS = SYSTEM_PROVIDER_DESCRIPTORS

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

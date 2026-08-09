"""Internal typing shared by local Relay capabilities."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import uuid4

from ..json_bounds import JsonValue
from ..output_models import ProviderToolResult
from ..protocol import InvokeMessage, ToolName
from ..provider_tools import ProviderToolDescriptor
from ..providers.base import UnknownProviderToolError

CapabilityName = ToolName


class LocalCapability(Protocol):
    tools: frozenset[ToolName]

    async def start(self) -> None: ...

    async def invoke(self, message: InvokeMessage) -> dict[str, object]: ...

    async def wait_unavailable(self) -> None: ...

    async def aclose(self) -> None: ...


class CapabilityProviderClient:
    """Expose one in-process local capability behind the provider client boundary."""

    def __init__(
        self,
        capability: LocalCapability,
        descriptors: Sequence[ProviderToolDescriptor] = (),
    ) -> None:
        self._capability = capability
        self._descriptors = tuple(descriptors)
        self._tool_names = {
            descriptor.tool_name: descriptor for descriptor in self._descriptors
        }
        if not self._tool_names:
            self._tool_names = {tool: None for tool in capability.tools}

    @property
    def wire_names(self) -> tuple[str, ...]:
        return tuple(
            (
                f"{descriptor.provider_name}.{descriptor.tool_name}"
                if descriptor is not None
                else tool_name
            )
            for tool_name, descriptor in self._tool_names.items()
        )

    async def list_tools(self) -> Sequence[ProviderToolDescriptor]:
        return self._descriptors

    async def call_tool(
        self, tool_name: str, arguments: Mapping[str, JsonValue]
    ) -> ProviderToolResult:
        return await self.call_message(
            tool_name,
            arguments,
            request_id=f"provider-{uuid4().hex}",
        )

    async def call_message(
        self,
        tool_name: str,
        arguments: Mapping[str, JsonValue],
        *,
        request_id: str,
    ) -> ProviderToolResult:
        descriptor = self._tool_names.get(tool_name)
        if tool_name not in self._tool_names:
            raise UnknownProviderToolError("unknown provider tool")
        wire_name = (
            f"{descriptor.provider_name}.{descriptor.tool_name}"
            if descriptor is not None
            else tool_name
        )
        result = await self._capability.invoke(
            InvokeMessage(
                version=2,
                type="invoke",
                request_id=request_id,
                tool_name=wire_name,
                arguments=dict(arguments),
            )
        )
        if isinstance(result, ProviderToolResult):
            return result
        return ProviderToolResult(content=[], structuredContent=result)

    async def close(self) -> None:
        # RelayAgent owns lifecycle for the underlying LocalCapability.
        return None


class CommandFailedError(RuntimeError):
    """The fixed runner reported a failure without exposing its details."""

    def __init__(self) -> None:
        super().__init__("configured command failed")

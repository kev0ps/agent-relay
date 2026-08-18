"""Dynamic MCP publication for selected provider descriptors."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any, Protocol
from uuid import uuid4

from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools.base import Tool
from mcp.server.mcpserver.tools.tool_manager import ToolManager
from mcp.types import CallToolResult
from pydantic import ValidationError

from .output_models import ProviderToolResult
from .protocol import InvokeMessage
from .provider_tools import ProviderToolDescriptor
from .providers.base import ProviderToolError, validate_provider_arguments
from .registry import (
    DeviceBusyError,
    DeviceOfflineError,
    LateResponseError,
    RemoteAgentError,
    UnknownDeviceError,
    UnsupportedToolError,
)


class DynamicRegistry(Protocol):
    @property
    def announced_descriptors(self) -> Mapping[str, ProviderToolDescriptor]: ...

    async def invoke(
        self,
        device_id: str | None,
        message: InvokeMessage,
        timeout_seconds: float,
    ) -> ProviderToolResult: ...


async def _unreachable_tool(**arguments: Any) -> dict[str, Any]:
    """Placeholder function; dynamic calls bypass MCPServer's static runner."""
    del arguments
    raise RuntimeError("dynamic tool placeholder was called")


class DynamicToolManager(ToolManager):
    """Project selected Agent descriptors into MCPServer's ToolManager surface.

    The manager keeps only fixed Server-local tools in its static collection. Provider
    tools are materialized from the current Registry announcement for each list/get
    operation, so an Agent restart or re-registration atomically changes publication.
    Calls resolve the public name back to the provider-qualified internal name and
    return a native MCP ``CallToolResult`` without semantic output conversion.
    """

    def __init__(
        self,
        *,
        registry: DynamicRegistry,
        timeout_seconds: float,
        device_id: str | None = None,
        static_tools: Sequence[Tool] = (),
        warn_on_duplicate_tools: bool = True,
    ) -> None:
        super().__init__(
            warn_on_duplicate_tools=warn_on_duplicate_tools,
            tools=list(static_tools),
        )
        self._registry = registry
        self._timeout_seconds = timeout_seconds
        self._device_id = device_id
        self._static_tools = {tool.name: tool for tool in static_tools}

    def list_tools(self) -> list[Tool]:
        dynamic = self._dynamic_tools()
        return [*self._static_tools.values(), *dynamic]

    def get_tool(self, name: str) -> Tool | None:
        static = self._static_tools.get(name)
        if static is not None:
            return static
        return self._dynamic_tool_by_name(name)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Any = None,
        convert_result: bool = False,
    ) -> CallToolResult | Any:
        if name in self._static_tools:
            return await super().call_tool(
                name,
                arguments,
                context=context,
                convert_result=convert_result,
            )

        descriptor = self._descriptor_by_public_name(name)
        if descriptor is None:
            raise ToolError("unknown or unselected provider tool")
        try:
            bounded_arguments = validate_provider_arguments(descriptor, arguments)
        except (ProviderToolError, TypeError, ValueError):
            raise ToolError("invalid tool arguments") from None

        message = InvokeMessage(
            version=2,
            type="invoke",
            request_id=uuid4().hex,
            tool_name=f"{descriptor.provider_name}.{descriptor.tool_name}",
            arguments=bounded_arguments,
        )
        try:
            result = await self._registry.invoke(
                self._device_id,
                message,
                self._timeout_seconds,
            )
        except UnknownDeviceError:
            raise ToolError("unknown device") from None
        except DeviceOfflineError:
            raise ToolError("device is offline") from None
        except DeviceBusyError:
            raise ToolError("device is busy") from None
        except UnsupportedToolError:
            raise ToolError("device does not support this capability") from None
        except (TimeoutError, LateResponseError):
            raise ToolError("device invocation timed out") from None
        except RemoteAgentError:
            raise ToolError("device invocation failed") from None
        except Exception:
            raise ToolError("internal relay error") from None
        return _to_mcp_result(result)

    def _dynamic_tools(self) -> list[Tool]:
        static_names = set(self._static_tools)
        tools: list[Tool] = []
        for descriptor in self._descriptors():
            if descriptor.public_name in static_names:
                raise RuntimeError("provider tool collides with a Server-local tool")
            tools.append(_tool_from_descriptor(descriptor))
        return tools

    def _dynamic_tool_by_name(self, name: str) -> Tool | None:
        for descriptor in self._descriptors():
            if descriptor.public_name == name:
                return _tool_from_descriptor(descriptor)
        return None

    def _descriptor_by_public_name(self, name: str) -> ProviderToolDescriptor | None:
        found: ProviderToolDescriptor | None = None
        for descriptor in self._descriptors():
            if descriptor.public_name != name:
                continue
            if found is not None:
                raise ToolError("provider tool name collision")
            found = descriptor
        return found

    def _descriptors(self) -> tuple[ProviderToolDescriptor, ...]:
        descriptors = tuple(self._registry.announced_descriptors.values())
        seen_public_names: set[str] = set()
        result: list[ProviderToolDescriptor] = []
        for descriptor in descriptors:
            if descriptor.public_name in seen_public_names:
                raise RuntimeError("provider tool name collision")
            seen_public_names.add(descriptor.public_name)
            result.append(descriptor)
        return tuple(result)


def _tool_from_descriptor(descriptor: ProviderToolDescriptor) -> Tool:
    tool = Tool.from_function(
        _unreachable_tool,
        name=descriptor.public_name,
        description=descriptor.description,
        structured_output=False,
        meta={"risk": descriptor.risk},
    )
    tool.parameters = deepcopy(descriptor.input_schema)
    tool.fn_metadata.output_schema = (
        deepcopy(descriptor.output_schema)
        if descriptor.output_schema is not None
        else None
    )
    return tool


def _to_mcp_result(result: ProviderToolResult) -> CallToolResult:
    try:
        bounded_result = ProviderToolResult.model_validate(result)
        return CallToolResult.model_validate(
            bounded_result.model_dump(mode="json", by_alias=True, exclude_none=True)
        )
    except (ValidationError, TypeError, ValueError):
        raise ToolError("device returned an invalid result") from None


__all__ = ["DynamicRegistry", "DynamicToolManager"]

"""Strict MCP facade for the single-device Relay server."""

from __future__ import annotations

import uuid
from ipaddress import ip_address
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError

from .config import SERVER_LOCAL_TOOL
from .mcp_dynamic_registry import DynamicToolManager
from .output_models import (
    Output,
    PingOutput,
    ProviderToolResult,
    TerminalExecOutput,
)
from .protocol import (
    CommandId,
    InvokeMessage,
)
from .registry import (
    DeviceBusyError,
    DeviceOfflineError,
    RelayRegistry,
    RemoteAgentError,
    UnknownDeviceError,
    UnsupportedToolError,
)


class DeviceStatusOutput(Output):
    device_id: str | None
    connected: bool
    capabilities: list[str]
    invocation_state: Literal["idle", "busy"]
    progress: int | None
    heartbeat_age_seconds: float | None


def create_mcp_facade(
    *,
    registry: RelayRegistry,
    timeout_seconds: float,
    device_id: str | None = None,
    only_announced: bool = False,
) -> MCPServer:
    """Create one MCP server for a Relay app."""
    mcp = MCPServer("Agent Relay")

    @mcp.tool(structured_output=True)
    async def relay_device_status() -> DeviceStatusOutput:
        """Return the configured Relay device's safe connection status."""
        try:
            snapshot = await registry.status_snapshot()
            return DeviceStatusOutput(
                device_id=snapshot.device_id,
                connected=snapshot.connected,
                capabilities=list(snapshot.capabilities),
                invocation_state=snapshot.invocation_state,
                progress=snapshot.progress,
                heartbeat_age_seconds=snapshot.heartbeat_age_seconds,
            )
        except ToolError:
            raise
        except Exception:
            raise ToolError("internal relay error") from None

    @mcp.tool(structured_output=True)
    async def relay_system_ping() -> PingOutput:
        """Invoke the fixed system ping capability on the configured device."""
        message = InvokeMessage(
            version=2,
            type="invoke",
            request_id=uuid.uuid4().hex,
            tool_name="system.ping",
            arguments={},
        )
        result = await _invoke(registry, device_id, message, timeout_seconds)
        return _validate_output(PingOutput, result)

    @mcp.tool(structured_output=True)
    async def relay_terminal_exec(command_id: CommandId) -> TerminalExecOutput:
        """Run one fixed, argument-free terminal command on the configured device."""
        message = InvokeMessage(
            version=2,
            type="invoke",
            request_id=uuid.uuid4().hex,
            tool_name="terminal.exec",
            arguments={"command_id": command_id},
        )
        result = await _invoke(registry, device_id, message, timeout_seconds)
        return _validate_output(TerminalExecOutput, result)

    _close_tool_input_schemas(mcp)
    if only_announced:
        server_tool = mcp._tool_manager.get_tool(SERVER_LOCAL_TOOL)
        if server_tool is None:
            raise RuntimeError("server-local MCP tool was not registered")
        mcp._tool_manager = DynamicToolManager(
            registry=registry,
            timeout_seconds=timeout_seconds,
            device_id=device_id,
            static_tools=(server_tool,),
            warn_on_duplicate_tools=mcp.settings.warn_on_duplicate_tools,
        )

    return mcp


def create_mcp_http_app(
    mcp: MCPServer,
    *,
    host: str = "127.0.0.1",
    allowed_hosts: tuple[str, ...] = (),
    allowed_origins: tuple[str, ...] = (),
    automatic_ip_host_policy: bool = False,
) -> Any:
    """Create the MCP 2 Streamable HTTP app with Relay transport policy."""
    transport_security = None
    if automatic_ip_host_policy:
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
    elif not _is_loopback_host(host) or allowed_hosts or allowed_origins:
        if not allowed_hosts:
            allowed_hosts = ("127.0.0.1:*", "localhost:*", "[::1]:*")
        if not allowed_origins:
            allowed_origins = (
                "http://127.0.0.1:*",
                "http://localhost:*",
                "http://[::1]:*",
            )
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(allowed_hosts),
            allowed_origins=list(allowed_origins),
        )
    return mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=False,
        json_response=True,
        transport_security=transport_security,
        host=host,
    )


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _close_tool_input_schemas(mcp: MCPServer[Any]) -> None:
    """Apply Relay's closed-schema contract across MCP SDK versions."""
    try:
        tool_manager = mcp._tool_manager
        registered_tools = tool_manager.list_tools()
        models = [tool.fn_metadata.arg_model for tool in registered_tools]
        if not all(
            isinstance(tool.parameters, dict)
            and isinstance(model.model_config, dict)
            and callable(model.model_rebuild)
            for tool, model in zip(registered_tools, models, strict=True)
        ):
            raise TypeError
    except (AttributeError, TypeError):
        raise RuntimeError("unsupported MCP SDK tool schema API") from None

    # The MCP SDK currently has no public override for the schemas generated
    # from these callable signatures. Keep every private SDK access contained here.
    for registered_tool, model in zip(registered_tools, models, strict=True):
        model.model_config["extra"] = "forbid"
        model.model_rebuild(force=True)
        registered_tool.parameters.pop("title", None)
        registered_tool.parameters["additionalProperties"] = False


async def _invoke(
    registry: RelayRegistry,
    device_id: str | None,
    message: InvokeMessage,
    timeout_seconds: float,
) -> ProviderToolResult:
    try:
        return await registry.invoke(device_id, message, timeout_seconds)
    except UnknownDeviceError:
        raise ToolError("unknown device") from None
    except DeviceOfflineError:
        raise ToolError("device is offline") from None
    except DeviceBusyError:
        raise ToolError("device is busy") from None
    except UnsupportedToolError:
        raise ToolError("device does not support this capability") from None
    except TimeoutError:
        raise ToolError("device invocation timed out") from None
    except RemoteAgentError:
        raise ToolError("device invocation failed") from None
    except Exception:
        raise ToolError("internal relay error") from None


def _validate_output(
    output_type: type[Output], result: ProviderToolResult
) -> Output:
    try:
        return output_type.model_validate(result.structured_content)
    except ValidationError:
        raise ToolError("device returned an invalid result") from None

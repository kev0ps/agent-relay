"""Strict MCP facade for the single-device Relay server."""

from __future__ import annotations

import uuid
from ipaddress import ip_address
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.tools.tool_manager import ToolManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field, ValidationError

from .config import INTERNAL_TO_PUBLIC, SERVER_LOCAL_TOOL
from .output_models import (
    BrowserActionOutput,
    BrowserPageOutput,
    BrowserTabsOutput,
    ComputerActionOutput,
    ComputerCaptureOutput,
    ComputerElementId,
    Output,
    PingOutput,
    TerminalExecOutput,
)
from .protocol import (
    MAX_BROWSER_ELEMENT_ID_LENGTH,
    MAX_BROWSER_FILL_VALUE_LENGTH,
    MAX_BROWSER_TYPE_TEXT_LENGTH,
    MAX_BROWSER_URL_LENGTH,
    BrowserBackInvoke,
    BrowserClickInvoke,
    BrowserFillInvoke,
    BrowserListTabsInvoke,
    BrowserNavigateInvoke,
    BrowserScrollInvoke,
    BrowserSnapshotInvoke,
    BrowserTypeInvoke,
    CommandId,
    ComputerCaptureInvoke,
    ComputerClickInvoke,
    ComputerTypeInvoke,
    ComputerTypeText,
    InvokeMessage,
    SystemPingInvoke,
    TerminalExecInvoke,
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


class _AnnouncedToolManager(ToolManager):
    """Expose only the tools declared by the currently connected Agent."""

    def __init__(
        self, registry: RelayRegistry, *, warn_on_duplicate_tools: bool = True
    ) -> None:
        super().__init__(warn_on_duplicate_tools=warn_on_duplicate_tools)
        self._registry = registry

    def _is_available(self, name: str) -> bool:
        if name == SERVER_LOCAL_TOOL:
            return True
        internal = next(
            (
                candidate
                for candidate, public in INTERNAL_TO_PUBLIC.items()
                if public == name
            ),
            None,
        )
        return (
            internal is not None
            and internal in self._registry.announced_capabilities
        )

    def list_tools(self) -> list[Any]:
        return [tool for tool in super().list_tools() if self._is_available(tool.name)]

    def get_tool(self, name: str) -> Any | None:
        if not self._is_available(name):
            return None
        return super().get_tool(name)


def create_mcp_facade(
    *,
    registry: RelayRegistry,
    timeout_seconds: float,
    device_id: str | None = None,
    host: str = "127.0.0.1",
    allowed_hosts: tuple[str, ...] = (),
    allowed_origins: tuple[str, ...] = (),
    only_announced: bool = False,
) -> FastMCP:
    """Create one stateless, JSON-response MCP server for a Relay app."""
    transport_security = None
    if not _is_loopback_host(host) or allowed_hosts or allowed_origins:
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
    mcp = FastMCP(
        "Agent Relay",
        host=host,
        stateless_http=False,
        json_response=True,
        streamable_http_path="/mcp",
        transport_security=transport_security,
    )
    if only_announced:
        mcp._tool_manager = _AnnouncedToolManager(
            registry,
            warn_on_duplicate_tools=mcp.settings.warn_on_duplicate_tools,
        )

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
        message = SystemPingInvoke(
            version=1,
            type="invoke",
            request_id=uuid.uuid4().hex,
            tool="system.ping",
        )
        result = await _invoke(registry, device_id, message, timeout_seconds)
        return _validate_output(PingOutput, result)

    @mcp.tool(structured_output=True)
    async def relay_terminal_exec(command_id: CommandId) -> TerminalExecOutput:
        """Run one fixed, argument-free terminal command on the configured device."""
        message = TerminalExecInvoke(
            version=1,
            type="invoke",
            request_id=uuid.uuid4().hex,
            tool="terminal.exec",
            command_id=command_id,
        )
        result = await _invoke(registry, device_id, message, timeout_seconds)
        return _validate_output(TerminalExecOutput, result)

    @mcp.tool(structured_output=True)
    async def relay_browser_list_tabs() -> BrowserTabsOutput:
        """List the bounded set of browser tabs available to the device."""
        message = BrowserListTabsInvoke(
            version=1,
            type="invoke",
            request_id=uuid.uuid4().hex,
            tool="browser.list_tabs",
        )
        return _validate_output(
            BrowserTabsOutput,
            await _invoke(registry, device_id, message, timeout_seconds),
        )

    @mcp.tool(structured_output=True)
    async def relay_browser_navigate(
        url: Annotated[str, Field(min_length=1, max_length=MAX_BROWSER_URL_LENGTH)],
    ) -> BrowserActionOutput:
        """Navigate the active browser tab to a bounded URL."""
        message = BrowserNavigateInvoke(
            version=1,
            type="invoke",
            request_id=uuid.uuid4().hex,
            tool="browser.navigate",
            url=url,
        )
        return _validate_output(
            BrowserActionOutput,
            await _invoke(registry, device_id, message, timeout_seconds),
        )

    @mcp.tool(structured_output=True)
    async def relay_browser_snapshot() -> BrowserPageOutput:
        """Return bounded semantic content from the active browser page."""
        message = BrowserSnapshotInvoke(
            version=1,
            type="invoke",
            request_id=uuid.uuid4().hex,
            tool="browser.snapshot",
        )
        return _validate_output(
            BrowserPageOutput,
            await _invoke(registry, device_id, message, timeout_seconds),
        )

    @mcp.tool(structured_output=True)
    async def relay_browser_fill(
        element_id: Annotated[
            str, Field(min_length=1, max_length=MAX_BROWSER_ELEMENT_ID_LENGTH)
        ],
        value: Annotated[
            str, Field(min_length=1, max_length=MAX_BROWSER_FILL_VALUE_LENGTH)
        ],
    ) -> BrowserActionOutput:
        """Fill one opaque semantic browser element with a bounded value."""
        message = BrowserFillInvoke(
            version=1,
            type="invoke",
            request_id=uuid.uuid4().hex,
            tool="browser.fill",
            element_id=element_id,
            value=value,
        )
        return _validate_output(
            BrowserActionOutput,
            await _invoke(registry, device_id, message, timeout_seconds),
        )

    @mcp.tool(structured_output=True)
    async def relay_browser_click(
        element_id: Annotated[
            str, Field(min_length=1, max_length=MAX_BROWSER_ELEMENT_ID_LENGTH)
        ],
    ) -> BrowserActionOutput:
        """Click one opaque semantic browser element."""
        message = BrowserClickInvoke(
            version=1,
            type="invoke",
            request_id=uuid.uuid4().hex,
            tool="browser.click",
            element_id=element_id,
        )
        return _validate_output(
            BrowserActionOutput,
            await _invoke(registry, device_id, message, timeout_seconds),
        )

    @mcp.tool(structured_output=True)
    async def relay_browser_scroll(direction: Literal["up", "down"]) -> BrowserActionOutput:
        """Scroll the active browser page by one viewport in a bounded direction."""
        message = BrowserScrollInvoke(
            version=1,
            type="invoke",
            request_id=uuid.uuid4().hex,
            tool="browser.scroll",
            direction=direction,
        )
        return _validate_output(
            BrowserActionOutput,
            await _invoke(registry, device_id, message, timeout_seconds),
        )

    @mcp.tool(structured_output=True)
    async def relay_browser_type(
        element_id: Annotated[
            str, Field(min_length=1, max_length=MAX_BROWSER_ELEMENT_ID_LENGTH)
        ],
        text: Annotated[
            str, Field(min_length=1, max_length=MAX_BROWSER_TYPE_TEXT_LENGTH)
        ],
    ) -> BrowserActionOutput:
        """Type bounded text into one opaque semantic browser element."""
        message = BrowserTypeInvoke(
            version=1,
            type="invoke",
            request_id=uuid.uuid4().hex,
            tool="browser.type",
            element_id=element_id,
            text=text,
        )
        return _validate_output(
            BrowserActionOutput,
            await _invoke(registry, device_id, message, timeout_seconds),
        )

    @mcp.tool(structured_output=True)
    async def relay_browser_back() -> BrowserActionOutput:
        """Navigate the active browser tab one step back in its history."""
        message = BrowserBackInvoke(
            version=1,
            type="invoke",
            request_id=uuid.uuid4().hex,
            tool="browser.back",
        )
        return _validate_output(
            BrowserActionOutput,
            await _invoke(registry, device_id, message, timeout_seconds),
        )

    @mcp.tool(structured_output=True)
    async def relay_computer_capture() -> ComputerCaptureOutput:
        """Capture bounded accessibility metadata and semantic elements."""
        message = ComputerCaptureInvoke(
            version=1,
            type="invoke",
            request_id=uuid.uuid4().hex,
            tool="computer.capture",
        )
        return _validate_output(
            ComputerCaptureOutput,
            await _invoke(registry, device_id, message, timeout_seconds),
        )

    @mcp.tool(structured_output=True)
    async def relay_computer_click(
        element_id: ComputerElementId,
    ) -> ComputerActionOutput:
        """Click one opaque element from the most recent capture generation."""
        message = ComputerClickInvoke(
            version=1,
            type="invoke",
            request_id=uuid.uuid4().hex,
            tool="computer.click",
            element_id=element_id,
        )
        return _validate_output(
            ComputerActionOutput,
            await _invoke(registry, device_id, message, timeout_seconds),
        )

    @mcp.tool(structured_output=True)
    async def relay_computer_type(
        element_id: ComputerElementId,
        text: ComputerTypeText,
    ) -> ComputerActionOutput:
        """Type bounded text into one opaque element from the latest capture."""
        message = ComputerTypeInvoke(
            version=1,
            type="invoke",
            request_id=uuid.uuid4().hex,
            tool="computer.type",
            element_id=element_id,
            text=text,
        )
        return _validate_output(
            ComputerActionOutput,
            await _invoke(registry, device_id, message, timeout_seconds),
        )

    _close_tool_input_schemas(mcp)

    return mcp


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _close_tool_input_schemas(mcp: FastMCP[Any]) -> None:
    """Apply the MCP v1 closed-schema compatibility shim or fail clearly."""
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

    # MCP v1 has no public override for the schemas generated from these
    # callable signatures. Keep every private SDK access contained here.
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
) -> dict[str, object]:
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


def _validate_output(output_type: type[Output], result: object) -> Output:
    try:
        return output_type.model_validate(result)
    except ValidationError:
        raise ToolError("device returned an invalid result") from None

"""Portable MCP client for Agent Relay.

This module isolates the ``tools/list`` and ``tools/call`` flows over the
official Streamable HTTP MCP transport. The shared scenarios make the
inventory assertion explicit, while the session also rejects drift during
initialization before any capability is invoked.

The portable client:

* accepts only loopback URLs (security invariant);
* accepts only tool names from the closed inventory;
* accepts only ``dict`` arguments (closed authority surface);
* runs the official Streamable HTTP client with a bounded timeout;
* raises ``MCPContractError`` if the server returns a different tool
  inventory than expected;
* raises ``ConnectionError`` on any transport / SDK failure so the
  harness can classify offline / busy / unsupported paths.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any, Final, Mapping
from urllib.parse import urlparse

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import (
    CallToolRequest,
    CallToolRequestParams,
    CallToolResult,
    ClientRequest,
)
from pydantic import ConfigDict

EXPECTED_MCP_TOOLS: Final[tuple[str, ...]] = (
    "relay_device_status",
    "relay_system_ping",
    "relay_terminal_exec",
    "relay_browser_list_tabs",
    "relay_browser_navigate",
    "relay_browser_read_page",
    "relay_browser_fill",
    "relay_browser_click",
    "relay_computer_capture",
    "relay_computer_click",
    "relay_computer_type",
)


class StrictCallToolResult(CallToolResult):
    """Validate the ``tools/call`` wire result before Pydantic coercion.

    ``extra='forbid'`` rejects any unknown wire fields, preserving the
    closed authority surface. ``strict=True`` forbids silent coercions
    (e.g. ``"1"`` becoming ``1``).
    """

    model_config = ConfigDict(extra="forbid", strict=True)


class MCPContractError(ValueError):
    """Server-side tool inventory drift; deterministic local error."""


_DEFAULT_HTTP_TIMEOUT_SECONDS: Final[float] = 5.0
_DEFAULT_OPERATION_TIMEOUT_SECONDS: Final[float] = 10.0


async def _ensure_loopback_endpoint_reachable(mcp_url: str, timeout: float) -> None:
    """Wait for an HTTP response before MCP creates a background task group."""
    parsed = urlparse(mcp_url)
    hostname = parsed.hostname
    port = parsed.port or 80
    if hostname is None:
        raise ConnectionError("relay MCP endpoint is unavailable")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    host_header = f"[{hostname}]" if hostname and ":" in hostname else hostname
    if port not in {80, 443}:
        host_header = f"{host_header}:{port}"
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(hostname, port),
            timeout=timeout,
        )
        writer.write(
            (
                f"HEAD {path} HTTP/1.1\r\n"
                f"Host: {host_header}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
        )
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        response_prefix = await asyncio.wait_for(reader.read(1), timeout=timeout)
        if not response_prefix:
            raise ConnectionError("relay MCP endpoint is unavailable")
    except (OSError, TimeoutError, ValueError):
        raise ConnectionError("relay MCP endpoint is unavailable") from None
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=timeout)
            except (OSError, TimeoutError):
                pass


class MCPClientSession:
    """One authenticated official MCP session for a portable scenario.

    The session owns the HTTP client, Streamable HTTP transport, SDK session,
    initialization, and closed tool-inventory check. Harnesses provide only
    the loopback endpoint and ephemeral control token; they never construct
    internal Relay frames.
    """

    def __init__(
        self,
        mcp_url: str,
        control_token: str,
        *,
        http_timeout: float = _DEFAULT_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self.mcp_url = mcp_url
        self.control_token = control_token
        self.http_timeout = http_timeout
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "MCPClientSession":
        _validate_inputs(
            self.mcp_url,
            self.control_token,
            "relay_device_status",
            {},
        )
        await _ensure_loopback_endpoint_reachable(self.mcp_url, self.http_timeout)
        stack = AsyncExitStack()
        self._stack = stack
        try:
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {self.control_token}"},
                    timeout=httpx.Timeout(self.http_timeout),
                    trust_env=False,
                )
            )
            read_stream, write_stream, _ = await stack.enter_async_context(
                streamable_http_client(
                    self.mcp_url,
                    http_client=http_client,
                )
            )
            session = await stack.enter_async_context(
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=self.http_timeout),
                )
            )
            await asyncio.wait_for(
                session.initialize(),
                timeout=self.http_timeout,
            )
            self._session = session
            await self.list_tools()
            return self
        except BaseException as error:
            if not isinstance(error, (asyncio.CancelledError, TimeoutError)):
                await stack.aclose()
            self._stack = None
            raise

    async def list_tools(self) -> tuple[str, ...]:
        """List and explicitly validate the public MCP tool inventory."""
        if self._session is None:
            raise RuntimeError("MCP client session is not open")
        discovered = tuple(
            tool.name
            for tool in (
                await asyncio.wait_for(
                    self._session.list_tools(),
                    timeout=self.http_timeout,
                )
            ).tools
        )
        if discovered != EXPECTED_MCP_TOOLS:
            raise MCPContractError("unexpected MCP tools")
        return discovered

    async def __aexit__(self, *exc_info: object) -> None:
        stack = self._stack
        self._session = None
        self._stack = None
        if stack is not None:
            exception_type = exc_info[0] if exc_info else None
            if isinstance(exception_type, type) and issubclass(
                exception_type, (asyncio.CancelledError, TimeoutError)
            ):
                # A canceled or timed-out operation must return to the harness
                # cleanup. Waiting for a transport close here can defeat the
                # caller's bound when the SDK/HTTP stack does not honor
                # cancellation promptly.
                return
            await stack.aclose()

    async def call(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> CallToolResult:
        """Call one discovered public tool in this initialized session."""
        _validate_inputs(self.mcp_url, self.control_token, tool_name, arguments)
        if self._session is None:
            raise RuntimeError("MCP client session is not open")
        return await asyncio.wait_for(
            self._session.send_request(
                ClientRequest(
                    CallToolRequest(
                        params=CallToolRequestParams(
                            name=tool_name,
                            arguments=dict(arguments),
                        )
                    )
                ),
                StrictCallToolResult,
            ),
            timeout=self.http_timeout,
        )


def _validate_inputs(
    mcp_url: str, control_token: str, tool_name: str, arguments: Mapping[str, Any]
) -> None:
    """Enforce the closed authority surface before any I/O."""
    if not isinstance(mcp_url, str) or not mcp_url:
        raise ValueError("mcp_url must be a non-empty string")
    parsed = urlparse(mcp_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("mcp_url must resolve to a loopback address")
    if parsed.scheme != "http":
        raise ValueError("mcp_url must use the http scheme on loopback")
    if not isinstance(control_token, str) or not control_token:
        raise ValueError("control_token must be a non-empty string")
    if tool_name not in EXPECTED_MCP_TOOLS:
        raise ValueError(f"tool name {tool_name!r} is not in the public inventory")
    if not isinstance(arguments, dict) or any(
        not isinstance(key, str) for key in arguments
    ):
        raise ValueError("arguments must be a dict[str, Any]")


async def call_tool_async(
    mcp_url: str,
    control_token: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    http_timeout: float = _DEFAULT_HTTP_TIMEOUT_SECONDS,
) -> CallToolResult:
    """Drive a single ``tools/call`` over Streamable HTTP.

    Parameters
    ----------
    mcp_url:
        Absolute loopback URL of the Relay Server ``/mcp`` endpoint.
    control_token:
        Bearer token the harness generated for this run.
    tool_name:
        Must be in :data:`tests.e2e.scenarios.EXPECTED_MCP_TOOLS`.
    arguments:
        JSON-serializable arguments. The portable client does NOT
        validate argument shape — the server-side facade does that and
        the oracles in ``tests.e2e.oracles`` verify the result.

    Returns
    -------
    A ``StrictCallToolResult``. Any extra wire field will already have
    raised ``ValidationError`` from Pydantic.

    Raises
    ------
    MCPContractError:
        The server's ``tools/list`` returned an inventory other than
        the expected one (drift = server-side contract change).
    ValueError:
        Inputs fail the closed authority surface checks.
    """
    _validate_inputs(mcp_url, control_token, tool_name, arguments)
    async with MCPClientSession(
        mcp_url,
        control_token,
        http_timeout=http_timeout,
    ) as session:
        return await session.call(tool_name, arguments)


def call_tool(
    mcp_url: str,
    control_token: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    http_timeout: float = _DEFAULT_HTTP_TIMEOUT_SECONDS,
    operation_timeout: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
) -> CallToolResult:
    """Synchronous wrapper around :func:`call_tool_async`.

    Transport / SDK failures are normalized to ``ConnectionError`` so
    the harness can classify them uniformly with offline / busy
    states. ``MCPContractError`` is re-raised unchanged.
    """
    _validate_inputs(mcp_url, control_token, tool_name, arguments)
    try:
        return asyncio.run(
            asyncio.wait_for(
                call_tool_async(
                    mcp_url,
                    control_token,
                    tool_name,
                    arguments,
                    http_timeout=http_timeout,
                ),
                timeout=operation_timeout,
            )
        )
    except MCPContractError:
        raise
    except asyncio.CancelledError:
        # The official Streamable HTTP transport can surface its operation
        # timeout as CancelledError while its background task group unwinds.
        # Synchronous harness readiness loops must treat that as a retryable
        # unavailable endpoint, not as cancellation of the harness itself.
        raise ConnectionError("relay MCP endpoint is unavailable") from None
    except ValueError:
        # Any ValueError after the input gate came from the transport/SDK
        # or from strict response validation. Do not expose wire details.
        raise ConnectionError("relay MCP endpoint is unavailable") from None
    except Exception:
        raise ConnectionError("relay MCP endpoint is unavailable") from None
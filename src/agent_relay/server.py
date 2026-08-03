"""FastAPI application factory for the temporary Relay control API."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from ipaddress import ip_address
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .auth import credentials_match
from .config import load_server_runtime
from .json_bounds import JsonObject, validate_json_bounds
from .mcp_facade import create_mcp_facade
from .output_models import ProviderToolResult
from .protocol import (
    MAX_TOKEN_LENGTH,
    AgentError,
    AgentResult,
    Capabilities,
    DeviceId,
    Heartbeat,
    InvokeMessage,
    Progress,
    Register,
    RequestId,
    ToolName,
    parse_agent_message,
)
from .registry import (
    AuthenticationError,
    DeviceAlreadyConnectedError,
    DeviceBusyError,
    DeviceOfflineError,
    LateResponseError,
    RelayRegistry,
    RemoteAgentError,
    UnknownDeviceError,
    UnknownRequestError,
    UnsupportedToolError,
)


class _SerializedWebSocket:
    """One async write gate for every outbound frame on a WS connection."""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._write_lock = asyncio.Lock()

    async def send_json(self, message: object) -> None:
        async with self._write_lock:
            await self._websocket.send_json(message)

    async def close(self, *, code: int, reason: str) -> None:
        async with self._write_lock:
            await self._websocket.close(code=code, reason=reason)


class _MCPBearerAuth:
    """Authenticate the one MCP route from raw ASGI headers."""

    def __init__(self, app: ASGIApp, mcp_token: str) -> None:
        self._app = app
        self._expected = f"Bearer {mcp_token}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") in {"/mcp", "/mcp/"}:
            values = [
                value
                for name, value in scope.get("headers", [])
                if name.lower() == b"authorization"
            ]
            supplied: str | None = None
            if len(values) == 1 and len(values[0]) <= len(b"Bearer ") + MAX_TOKEN_LENGTH:
                try:
                    supplied = values[0].decode("ascii")
                except UnicodeDecodeError:
                    supplied = None
            if supplied is None or not credentials_match(supplied, self._expected):
                response = JSONResponse(
                    {"detail": "authentication required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


def _websocket_bearer_matches(websocket: WebSocket, expected: str) -> bool:
    """Validate exactly one bounded ASCII Bearer header without exposing it."""
    values = [
        value
        for name, value in websocket.scope.get("headers", [])
        if name.lower() == b"authorization"
    ]
    if len(values) != 1 or len(values[0]) > len(b"Bearer ") + MAX_TOKEN_LENGTH:
        return False
    try:
        supplied = values[0].decode("ascii")
    except UnicodeDecodeError:
        return False
    return credentials_match(supplied, expected)


class RelaySettings(BaseModel):
    """Explicit deployment settings; callers supply both independent secrets."""

    model_config = ConfigDict(extra="forbid", strict=True)

    agent_token: Annotated[str, Field(min_length=1, max_length=MAX_TOKEN_LENGTH)] = (
        Field(repr=False)
    )
    mcp_token: Annotated[str, Field(min_length=1, max_length=MAX_TOKEN_LENGTH)] = (
        Field(repr=False)
    )
    # A server can start before any Agent has registered.
    device_id: DeviceId | None = None
    bind_host: str = "0.0.0.0"
    mcp_allowed_hosts: tuple[str, ...] = ()
    mcp_allowed_origins: tuple[str, ...] = ()
    allow_insecure_ws: bool = True
    min_timeout_seconds: Annotated[float, Field(gt=0, le=3600)] = 0.1
    max_timeout_seconds: Annotated[float, Field(gt=0, le=3600)] = 30.0
    cancel_send_timeout_seconds: Annotated[float, Field(gt=0, le=5)] = 0.25
    # Keep this aligned with Uvicorn's ws_max_size when that server setting is
    # introduced in Lot D; it is enforced here before JSON decoding.
    max_ws_message_bytes: Annotated[int, Field(ge=1024, le=1024 * 1024)] = 128 * 1024

    def __init__(self, /, **data: object) -> None:
        try:
            super().__init__(**data)
        except ValidationError:
            # Pydantic includes rejected input values in its normal error text.
            # Server settings contain two credentials, so expose no raw input.
            raise ValueError("invalid relay server configuration") from None

    @classmethod
    def model_validate(cls, *args: object, **kwargs: object) -> RelaySettings:
        try:
            return super().model_validate(*args, **kwargs)
        except ValidationError:
            raise ValueError("invalid relay server configuration") from None

    @classmethod
    def model_validate_json(cls, *args: object, **kwargs: object) -> RelaySettings:
        try:
            return super().model_validate_json(*args, **kwargs)
        except ValidationError:
            raise ValueError("invalid relay server configuration") from None

    @classmethod
    def model_validate_strings(cls, *args: object, **kwargs: object) -> RelaySettings:
        try:
            return super().model_validate_strings(*args, **kwargs)
        except ValidationError:
            raise ValueError("invalid relay server configuration") from None

    @model_validator(mode="after")
    def valid_timeout_range(self) -> RelaySettings:
        if self.min_timeout_seconds > self.max_timeout_seconds:
            raise ValueError("min_timeout_seconds must be <= max_timeout_seconds")
        if credentials_match(self.agent_token, self.mcp_token):
            raise ValueError("agent and control tokens must differ")
        return self


    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        bind_host: str | None = None,
    ) -> RelaySettings:
        """Load canonical server settings without requiring an Agent identity."""
        env = os.environ if environ is None else environ
        try:
            values: dict[str, object] = {
                "agent_token": env["RELAY_AGENT_TOKEN"],
                "mcp_token": env["RELAY_MCP_TOKEN"],
                "bind_host": bind_host
                if bind_host is not None
                else env.get("RELAY_SERVER_HOST", "0.0.0.0"),
                "allow_insecure_ws": _parse_bool(
                    env.get("RELAY_ALLOW_INSECURE_WS", "true")
                ),
                "mcp_allowed_hosts": _split_csv(
                    env.get("RELAY_MCP_ALLOWED_HOSTS", "")
                ),
                "mcp_allowed_origins": _split_csv(
                    env.get("RELAY_MCP_ALLOWED_ORIGINS", "")
                ),
            }
            if "RELAY_MAX_WS_MESSAGE_BYTES" in env:
                values["max_ws_message_bytes"] = int(env["RELAY_MAX_WS_MESSAGE_BYTES"])
            return cls(**values)
        except (KeyError, TypeError, ValueError):
            raise ValueError("invalid relay server configuration") from None


class InvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tool_name: ToolName
    arguments: JsonObject = Field(default_factory=dict)
    timeout_seconds: Annotated[float | None, Field(gt=0, le=3600)] = None

    @field_validator("arguments", mode="before")
    @classmethod
    def bounded_arguments(cls, value: object) -> object:
        return validate_json_bounds(value, require_object=True, label="arguments")


class InvokeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: RequestId
    result: ProviderToolResult


def create_app(settings: RelaySettings) -> FastAPI:
    """Create a Relay server without reading or embedding any secrets."""
    registry = RelayRegistry(
        device_id=settings.device_id,
        agent_token=settings.agent_token,
        cancel_send_timeout_seconds=settings.cancel_send_timeout_seconds,
    )
    mcp = create_mcp_facade(
        registry=registry,
        timeout_seconds=settings.max_timeout_seconds,
        host=settings.bind_host,
        allowed_hosts=settings.mcp_allowed_hosts,
        allowed_origins=settings.mcp_allowed_origins,
        only_announced=True,
    )
    mcp_http_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def sanitized_request_validation_error(
        _request: object, _exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse({"detail": "invalid request"}, status_code=422)
    app.state.registry = registry
    app.state.settings = settings
    app.state.mcp = mcp

    @app.websocket("/ws/agent")
    async def agent_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        if not _websocket_bearer_matches(
            websocket, f"Bearer {settings.agent_token}"
        ):
            await websocket.close(code=1008, reason="authentication failed")
            return
        connection = _SerializedWebSocket(websocket)
        registered = False
        try:
            while True:
                try:
                    frame = await websocket.receive()
                    if frame["type"] == "websocket.disconnect":
                        raise WebSocketDisconnect(frame.get("code", 1000))
                    if frame.get("bytes") is not None:
                        await connection.close(
                            code=1002, reason="binary frames are not allowed"
                        )
                        return
                    text = frame.get("text")
                    if not isinstance(text, str):
                        await connection.close(code=1002, reason="invalid protocol message")
                        return
                    if len(text.encode("utf-8")) > settings.max_ws_message_bytes:
                        await connection.close(code=1009, reason="message too large")
                        return
                    raw = json.loads(text)
                    message = parse_agent_message(raw)
                except (ValueError, RecursionError, ValidationError):
                    await connection.close(code=1002, reason="invalid protocol message")
                    return
                if not registered:
                    if not isinstance(message, Register):
                        await connection.close(
                            code=1002, reason="register required first"
                        )
                        return
                    try:
                        reply = await registry.register(connection, message)
                    except AuthenticationError:
                        await connection.close(code=1008, reason="authentication failed")
                        return
                    except DeviceAlreadyConnectedError:
                        await connection.close(code=1013, reason="device already connected")
                        return
                    await registry.send(connection, reply.model_dump(mode="json"))
                    registered = True
                    continue
                try:
                    if isinstance(message, Capabilities):
                        await registry.set_capabilities(connection, message)
                    elif isinstance(message, Heartbeat):
                        await registry.heartbeat(connection)
                    elif isinstance(message, AgentResult):
                        await registry.handle_result(message)
                    elif isinstance(message, AgentError):
                        await registry.handle_error(message)
                    elif isinstance(message, Progress):
                        await registry.handle_progress(message)
                    else:
                        await connection.close(
                            code=1002, reason="unexpected agent message"
                        )
                        return
                except LateResponseError:
                    await connection.close(
                        code=1002, reason="late or duplicate response"
                    )
                    return
                except (AuthenticationError, UnknownRequestError):
                    await connection.close(
                        code=1002, reason="invalid request correlation"
                    )
                    return
        except WebSocketDisconnect:
            pass
        finally:
            await registry.disconnect(connection)

    @app.post("/v1/devices/{device_id}/invoke")
    async def invoke(
        device_id: str,
        body: InvokeRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> InvokeResponse:
        expected = f"Bearer {settings.mcp_token}"
        if authorization is None or not credentials_match(authorization, expected):
            raise HTTPException(
                status_code=401,
                detail="authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        timeout = body.timeout_seconds or settings.max_timeout_seconds
        timeout = min(
            max(timeout, settings.min_timeout_seconds), settings.max_timeout_seconds
        )
        request_id = uuid.uuid4().hex
        message = InvokeMessage(
            version=2,
            type="invoke",
            request_id=request_id,
            tool_name=body.tool_name,
            arguments=body.arguments,
        )
        try:
            result = await registry.invoke(device_id, message, timeout)
        except DeviceOfflineError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except UnknownDeviceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DeviceBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UnsupportedToolError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(
                status_code=504, detail="agent invocation timed out"
            ) from exc
        except RemoteAgentError as exc:
            raise HTTPException(
                status_code=502, detail={"code": exc.code, "message": str(exc)}
            ) from exc
        try:
            validated = ProviderToolResult.model_validate(result)
        except ValidationError:
            raise HTTPException(
                status_code=502, detail="device returned an invalid result"
            ) from None
        return InvokeResponse(request_id=request_id, result=validated)

    # Mount last so the existing control and WebSocket routes retain priority.
    # The child owns /mcp directly, avoiding a /mcp -> /mcp/ redirect.
    app.mount("/", _MCPBearerAuth(mcp_http_app, settings.mcp_token))

    return app


def main(argv: Sequence[str] | None = None) -> None:
    """Run the Relay server from YAML, with canonical Docker env overrides."""
    env = os.environ
    parser = argparse.ArgumentParser(description="Agent Relay control server")
    parser.add_argument("--config", type=str)
    parser.add_argument("--host")
    parser.add_argument("--port")
    args = parser.parse_args(argv)
    try:
        if args.config is not None:
            runtime = load_server_runtime(args.config, env=env)
            settings = runtime.settings
            host = runtime.host
            port = runtime.port
        else:
            host = args.host or env.get("RELAY_SERVER_HOST", "0.0.0.0")
            port = int(args.port or env.get("RELAY_SERVER_PORT", "8000"))
            if not 1 <= port <= 65535 or not _is_canonical_bind_host(host):
                raise ValueError
            settings = RelaySettings.from_environment(env, bind_host=host)
    except (KeyError, TypeError, ValueError, OSError):
        parser.error("invalid relay server configuration")
    import uvicorn

    uvicorn.run(
        create_app(settings),
        host=host,
        port=port,
        ws_max_size=settings.max_ws_message_bytes,
    )


def _is_loopback_bind_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _is_canonical_bind_host(host: str) -> bool:
    """Allow loopback and the documented LAN wildcard binds for canonical config."""
    return _is_loopback_bind_host(host) or host in {"0.0.0.0", "::"}


def _is_allowed_bind_host(
    host: str,
    *,
    allow_non_loopback: bool,
    mcp_allowed_hosts: tuple[str, ...],
) -> bool:
    if _is_loopback_bind_host(host):
        return True
    return (
        allow_non_loopback
        and host in {"0.0.0.0", "::"}
        and bool(mcp_allowed_hosts)
    )


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError


if __name__ == "__main__":
    main()

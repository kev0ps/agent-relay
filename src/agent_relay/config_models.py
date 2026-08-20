"""Canonical Pydantic models for Agent Relay YAML configuration."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, ClassVar
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .catalog import CatalogError, validate_agent_allowlist


def _config_int(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not an integer")
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid integer") from exc


def _config_float(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid number") from exc


ConfigInt = Annotated[int, BeforeValidator(_config_int)]
ConfigFloat = Annotated[float, BeforeValidator(_config_float)]


def _set_nested(document: dict[str, Any], path: str, value: object) -> None:
    current = document
    parts = path.split(".")
    for part in parts[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            return
        current = child
    current[parts[-1]] = value


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class ConfigModel(BaseModel):
    """Closed configuration model with declarative environment overlays."""

    model_config = ConfigDict(extra="forbid", validate_default=True)
    env_fields: ClassVar[dict[str, tuple[str, Any | None]]] = {}

    @classmethod
    def from_sources(
        cls,
        raw: Mapping[str, Any],
        env: Mapping[str, str],
    ):
        values = copy.deepcopy(dict(raw))
        for env_name, (field_path, parser) in cls.env_fields.items():
            if env_name in env:
                value: object = env[env_name]
                if parser is not None:
                    value = parser(env[env_name])
                _set_nested(values, field_path, value)
        return cls.model_validate(values)


class ServerMcpConfig(ConfigModel):
    allowed_hosts: list[str] = Field(default_factory=list)
    allowed_origins: list[str] = Field(default_factory=list)


class ServerRuntimeConfig(ConfigModel):
    min_timeout_seconds: ConfigFloat = Field(default=0.1, gt=0, le=3600)
    max_timeout_seconds: ConfigFloat = Field(default=30.0, gt=0, le=3600)
    cancel_send_timeout_seconds: ConfigFloat = Field(default=0.25, gt=0, le=5)
    max_ws_message_bytes: ConfigInt = Field(
        default=128 * 1024, ge=1024, le=1024 * 1024
    )

    @model_validator(mode="after")
    def valid_timeout_range(self) -> ServerRuntimeConfig:
        if self.min_timeout_seconds > self.max_timeout_seconds:
            raise ValueError("minimum timeout exceeds maximum timeout")
        return self


class ServerConfig(ConfigModel):
    host: str = "127.0.0.1"
    port: ConfigInt = Field(default=8000, ge=1, le=65535)
    mcp: ServerMcpConfig = Field(default_factory=ServerMcpConfig)
    runtime: ServerRuntimeConfig = Field(default_factory=ServerRuntimeConfig)

    env_fields = {
        "RELAY_SERVER_HOST": ("host", None),
        "RELAY_SERVER_PORT": ("port", None),
        "RELAY_MCP_ALLOWED_HOSTS": ("mcp.allowed_hosts", _csv),
        "RELAY_MCP_ALLOWED_ORIGINS": ("mcp.allowed_origins", _csv),
        "RELAY_MIN_TIMEOUT_SECONDS": ("runtime.min_timeout_seconds", None),
        "RELAY_MAX_TIMEOUT_SECONDS": ("runtime.max_timeout_seconds", None),
        "RELAY_CANCEL_SEND_TIMEOUT_SECONDS": (
            "runtime.cancel_send_timeout_seconds",
            None,
        ),
        "RELAY_MAX_WS_MESSAGE_BYTES": ("runtime.max_ws_message_bytes", None),
    }

    @field_validator("host")
    @classmethod
    def valid_host(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("host must not be empty")
        return value

    def runtime_settings(self, *, mcp_token: str, agent_token: str) -> dict[str, Any]:
        values: dict[str, Any] = {
            "agent_token": agent_token,
            "mcp_token": mcp_token,
            "bind_host": self.host,
            "mcp_allowed_hosts": tuple(self.mcp.allowed_hosts),
            "mcp_allowed_origins": tuple(self.mcp.allowed_origins),
        }
        values.update(self.runtime.model_dump())
        return values


class AgentIdentityConfig(ConfigModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    @field_validator("id")
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        try:
            uuid.UUID(value)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("identity must be a UUID") from exc
        return value


class AgentToolsConfig(ConfigModel):
    allowlist: list[str] = Field(default_factory=list)

    @field_validator("allowlist")
    @classmethod
    def valid_names(cls, value: list[str]) -> list[str]:
        try:
            validate_agent_allowlist(value, defer_unknown=True)
        except CatalogError as exc:
            raise ValueError(str(exc)) from exc
        return value


class AgentComputerConfig(ConfigModel):
    allowed_app_name: str | None = Field(default=None, min_length=1, max_length=128)
    allowed_window_title: str | None = Field(default=None, min_length=1, max_length=256)
    startup_timeout_seconds: ConfigFloat = Field(default=15.0, gt=0, le=30)
    action_timeout_seconds: ConfigFloat = Field(default=10.0, gt=0, le=30)
    shutdown_timeout_seconds: ConfigFloat = Field(default=3.0, gt=0, le=30)
    max_elements: ConfigInt = Field(default=300, ge=1, le=1000)

    @model_validator(mode="after")
    def complete_policy(self) -> AgentComputerConfig:
        values = (self.allowed_app_name, self.allowed_window_title)
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError("computer policy fields must be provided together")
        return self


class AgentRuntimeConfig(ConfigModel):
    heartbeat_interval_seconds: ConfigFloat = Field(default=15.0, gt=0, le=3600)
    reconnect_min_seconds: ConfigFloat = Field(default=0.1, gt=0, le=60)
    reconnect_max_seconds: ConfigFloat = Field(default=5.0, gt=0, le=3600)
    stable_session_seconds: ConfigFloat = Field(default=30.0, ge=1, le=3600)
    max_ws_message_bytes: ConfigInt = Field(
        default=128 * 1024, ge=1024, le=1024 * 1024
    )
    command_timeout_seconds: ConfigFloat = Field(default=30.0, gt=0, le=3600)
    stdout_limit: ConfigInt = Field(default=24 * 1024, ge=0, le=48 * 1024)
    stderr_limit: ConfigInt = Field(default=24 * 1024, ge=0, le=48 * 1024)

    @model_validator(mode="after")
    def valid_reconnect_range(self) -> AgentRuntimeConfig:
        if self.reconnect_min_seconds > self.reconnect_max_seconds:
            raise ValueError("minimum reconnect delay exceeds maximum")
        return self


class AgentConfig(ConfigModel):
    identity: AgentIdentityConfig = Field(default_factory=AgentIdentityConfig)
    relay_url: str = "ws://127.0.0.1:8000/ws/agent"
    workspace: str = "./workspace"
    tools: AgentToolsConfig = Field(default_factory=AgentToolsConfig)
    computer: AgentComputerConfig = Field(default_factory=AgentComputerConfig)
    runtime: AgentRuntimeConfig = Field(default_factory=AgentRuntimeConfig)

    env_fields = {
        "RELAY_URL": ("relay_url", None),
        "RELAY_AGENT_ID": ("identity.id", None),
        "RELAY_AGENT_WORKSPACE": ("workspace", None),
        "RELAY_AGENT_TOOLS": ("tools.allowlist", _csv),
        "RELAY_AGENT_HEARTBEAT_INTERVAL_SECONDS": (
            "runtime.heartbeat_interval_seconds",
            None,
        ),
        "RELAY_AGENT_RECONNECT_MIN_SECONDS": ("runtime.reconnect_min_seconds", None),
        "RELAY_AGENT_RECONNECT_MAX_SECONDS": ("runtime.reconnect_max_seconds", None),
        "RELAY_AGENT_STABLE_SESSION_SECONDS": ("runtime.stable_session_seconds", None),
        "RELAY_AGENT_MAX_WS_MESSAGE_BYTES": ("runtime.max_ws_message_bytes", None),
        "RELAY_AGENT_COMMAND_TIMEOUT_SECONDS": ("runtime.command_timeout_seconds", None),
        "RELAY_AGENT_STDOUT_LIMIT": ("runtime.stdout_limit", None),
        "RELAY_AGENT_STDERR_LIMIT": ("runtime.stderr_limit", None),
        "RELAY_AGENT_COMPUTER_ALLOWED_APP_NAME": ("computer.allowed_app_name", None),
        "RELAY_AGENT_COMPUTER_ALLOWED_WINDOW_TITLE": (
            "computer.allowed_window_title",
            None,
        ),
        "RELAY_AGENT_COMPUTER_STARTUP_TIMEOUT_SECONDS": (
            "computer.startup_timeout_seconds",
            None,
        ),
        "RELAY_AGENT_COMPUTER_ACTION_TIMEOUT_SECONDS": (
            "computer.action_timeout_seconds",
            None,
        ),
        "RELAY_AGENT_COMPUTER_SHUTDOWN_TIMEOUT_SECONDS": (
            "computer.shutdown_timeout_seconds",
            None,
        ),
        "RELAY_AGENT_COMPUTER_MAX_ELEMENTS": ("computer.max_elements", None),
    }

    @field_validator("relay_url")
    @classmethod
    def valid_relay_url(cls, value: str) -> str:
        try:
            parsed = urlparse(value)
            parsed.port
        except ValueError as exc:
            raise ValueError("invalid relay URL") from exc
        if (
            parsed.scheme not in {"ws", "wss"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("relay URL must use ws:// or wss://")
        return value

    @field_validator("workspace")
    @classmethod
    def nonempty_workspace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("workspace must not be empty")
        return value

    def resolve_workspace(self, config_path: Path) -> Path:
        path = Path(self.workspace).expanduser()
        if not path.is_absolute():
            path = config_path.parent / path
        return path.resolve(strict=False)

    def runtime_settings(self, *, token: str, config_path: Path) -> dict[str, Any]:
        identity = self.identity.id
        values: dict[str, Any] = {
            "server_url": self.relay_url,
            "device_id": identity,
            "agent_id": identity,
            "agent_token": token,
            "workspace": self.resolve_workspace(config_path),
            "tools_allowlist": tuple(self.tools.allowlist),
        }
        values.update(self.runtime.model_dump())
        values.update(
            {
                f"computer_{name}": value
                for name, value in self.computer.model_dump().items()
            }
        )
        return values


def configuration_keys(model: type[ConfigModel]) -> frozenset[str]:
    """Derive dotted CLI keys from the canonical model tree."""

    keys: set[str] = set()

    def walk(current: type[ConfigModel], prefix: str = "") -> None:
        for name, field in current.model_fields.items():
            path = f"{prefix}.{name}" if prefix else name
            annotation = field.annotation
            if isinstance(annotation, type) and issubclass(annotation, ConfigModel):
                walk(annotation, path)
            else:
                keys.add(path)

    walk(model)
    return frozenset(keys)


__all__ = [
    "AgentComputerConfig",
    "AgentConfig",
    "AgentIdentityConfig",
    "AgentRuntimeConfig",
    "AgentToolsConfig",
    "ConfigModel",
    "ServerConfig",
    "ServerMcpConfig",
    "ServerRuntimeConfig",
    "configuration_keys",
]

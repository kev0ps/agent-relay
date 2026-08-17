"""Outbound Linux Relay agent and its deliberately small local configuration."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import re
import secrets
import signal
import stat
import time
from collections.abc import Coroutine, Mapping, Sequence
from ipaddress import ip_address
from pathlib import Path
from typing import Any, AsyncContextManager, Callable, Protocol, cast
from urllib.parse import urlparse

import websockets
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from .capabilities.base import (
    CapabilityName,
    CapabilityProviderClient,
    CommandFailedError,
    LocalCapability,
)
from .capabilities.system import SYSTEM_PROVIDER_DESCRIPTORS, SystemCapability
from .capabilities.terminal import (
    TERMINAL_PROVIDER_DESCRIPTORS,
    CommandRunnerProtocol,
    TerminalCapability,
)
from .catalog import (
    CatalogError,
    CatalogService,
    CatalogSnapshot,
    local_provider_registrations,
)
from .config import PUBLIC_TO_INTERNAL, load_agent_settings
from .diagnostics import debug as _debug_log
from .diagnostics import info as _info_log
from .protocol import (
    MAX_RESULT_JSON_BYTES,
    TOOL_ORDER,
    AgentError,
    AgentResult,
    Cancel,
    Capabilities,
    Heartbeat,
    InvokeMessage,
    Registered,
    ToolName,
    parse_server_message,
)
from .provider_tools import ProviderToolDescriptor
from .providers.base import (
    ProviderToolClient,
    bounded_result,
    validate_provider_arguments,
)
from .runner import CommandRunner


class ConfigurationError(ValueError):
    """A deliberately non-descriptive error for local agent configuration."""

    def __init__(self) -> None:
        super().__init__("invalid agent configuration")


def _debug_configuration_validation(error: ValidationError) -> None:
    """Report only rejected field locations when native diagnostics are enabled."""
    if os.environ.get("RELAY_NATIVE_DEBUG") != "1":
        return
    locations = sorted(
        ".".join(str(part) for part in item.get("loc", ())) or "<root>"
        for item in error.errors()
    )
    _debug_log("agent configuration rejected fields: " + ", ".join(locations))


class ProviderUnavailableError(ConnectionError):
    """A selected provider became unavailable during an Agent session."""


def _debug_agent_phase(phase: str) -> None:
    _debug_log(f"agent lifecycle phase: {phase}")


def _operator_agent_info(message: str) -> None:
    """Emit concise lifecycle information without enabling native diagnostics."""
    _info_log(message)


def safe_server_target(value: str) -> str:
    """Render only the scheme, host, and port from a configured Relay URL."""
    try:
        parsed = urlparse(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return "<configured Relay>"
    if not parsed.scheme or not host:
        return "<configured Relay>"
    display_host = f"[{host}]" if ":" in host else host
    display_port = f":{port}" if port is not None else ""
    return f"{parsed.scheme}://{display_host}{display_port}"


def _configured_computer_provider(
    settings: AgentSettings,
) -> ProviderToolClient | None:
    from .capabilities.computer import ComputerCapability

    try:
        return ComputerCapability(
            settings.computer_allowed_app_name,
            settings.computer_allowed_window_title,
            startup_timeout_seconds=settings.computer_startup_timeout_seconds,
            action_timeout_seconds=settings.computer_action_timeout_seconds,
            shutdown_timeout_seconds=settings.computer_shutdown_timeout_seconds,
            max_elements=settings.computer_max_elements,
        )
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        _debug_log("cua provider construction failed")
        return None


class AgentSettings(BaseModel):
    """Settings controlled only by the local operator, never by INVOKE frames."""

    model_config = ConfigDict(extra="forbid", strict=True)

    server_url: str
    device_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    agent_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$"
    )
    agent_token: SecretStr = Field(repr=False, min_length=1, max_length=256)
    workspace: Path
    allow_insecure_ws: bool = False
    heartbeat_interval_seconds: float = Field(default=15, gt=0, le=3600)
    reconnect_min_seconds: float = Field(default=0.1, gt=0, le=60)
    reconnect_max_seconds: float = Field(default=5, gt=0, le=3600)
    stable_session_seconds: float = Field(default=30, ge=1, le=3600)
    max_ws_message_bytes: int = Field(default=128 * 1024, ge=1024, le=1024 * 1024)
    command_timeout_seconds: float = Field(default=30, gt=0, le=3600)
    stdout_limit: int = Field(default=24 * 1024, ge=0, le=48 * 1024)
    stderr_limit: int = Field(default=24 * 1024, ge=0, le=48 * 1024)
    # ``None`` preserves the programmatic API's historical all-configured
    # behavior. YAML always supplies a tuple, including an empty tuple.
    tools_allowlist: tuple[str, ...] | None = None
    computer_allowed_app_name: str | None = Field(default=None, min_length=1, max_length=128)
    computer_allowed_window_title: str | None = Field(default=None, min_length=1, max_length=256)
    computer_startup_timeout_seconds: float = Field(default=15, gt=0, le=30)
    computer_action_timeout_seconds: float = Field(default=10, gt=0, le=30)
    computer_shutdown_timeout_seconds: float = Field(default=3, gt=0, le=30)
    computer_max_elements: int = Field(default=300, ge=1, le=1000)
    def __init__(self, /, **data: object) -> None:
        try:
            super().__init__(**data)
        except ValidationError as error:
            # Pydantic's default rendering includes rejected input values.  Those
            # values can be credentials, so never expose the original error.
            _debug_configuration_validation(error)
            raise ConfigurationError() from None

    @classmethod
    def model_validate(cls, *args: object, **kwargs: object) -> AgentSettings:
        try:
            return super().model_validate(*args, **kwargs)
        except ValidationError as error:
            _debug_configuration_validation(error)
            raise ConfigurationError() from None

    @classmethod
    def model_validate_json(cls, *args: object, **kwargs: object) -> AgentSettings:
        try:
            return super().model_validate_json(*args, **kwargs)
        except ValidationError as error:
            _debug_configuration_validation(error)
            raise ConfigurationError() from None

    @classmethod
    def model_validate_strings(cls, *args: object, **kwargs: object) -> AgentSettings:
        try:
            return super().model_validate_strings(*args, **kwargs)
        except ValidationError as error:
            _debug_configuration_validation(error)
            raise ConfigurationError() from None

    @field_validator("server_url")
    @classmethod
    def valid_server_url(cls, value: str) -> str:
        try:
            parsed = urlparse(value)
            parsed.port
        except ValueError as error:
            raise ValueError("invalid server_url") from error
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise ValueError("server_url must be a ws:// or wss:// URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("server_url must not include userinfo")
        # The endpoint path is intentionally not part of the configuration
        # contract; a future Relay protocol may move it.
        if parsed.fragment:
            raise ValueError("server_url must not include a fragment")
        return value

    @field_validator("workspace")
    @classmethod
    def local_workspace(cls, value: Path) -> Path:
        if not value.is_absolute() or not value.is_dir() or value.is_symlink():
            raise ValueError("workspace must be an absolute existing non-symlink directory")
        return value.resolve(strict=True)

    @model_validator(mode="after")
    def secure_url_and_ranges(self) -> AgentSettings:
        if self.agent_id is None:
            self.agent_id = self.device_id
        elif self.agent_id != self.device_id:
            raise ValueError("device_id and agent_id must match")
        parsed = urlparse(self.server_url)
        if parsed.scheme == "ws" and not _is_explicit_loopback(parsed.hostname or ""):
            if not self.allow_insecure_ws:
                raise ValueError("ws:// is permitted only for explicit loopback hosts")
        if self.reconnect_min_seconds > self.reconnect_max_seconds:
            raise ValueError("reconnect_min_seconds must be <= reconnect_max_seconds")
        result_budget = min(MAX_RESULT_JSON_BYTES, self.max_ws_message_bytes) - 2048
        if self.stdout_limit + self.stderr_limit > result_budget:
            raise ValueError("combined output limits exceed the protocol message budget")
        computer_values = (
            self.computer_allowed_app_name,
            self.computer_allowed_window_title,
        )
        if any(value is not None for value in computer_values) and not all(
            value is not None for value in computer_values
        ):
            raise ValueError("partial computer configuration")
        return self

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        defer_tool_validation: bool = False,
    ) -> AgentSettings:
        try:
            env = os.environ if environ is None else environ
            values = _canonical_agent_values(
                env, defer_tool_validation=defer_tool_validation
            )
            return cls(**values)
        except (ConfigurationError, OSError, ValueError, TypeError):
            raise ConfigurationError() from None




_AGENT_OPTION_FIELDS = (
    "heartbeat_interval_seconds",
    "reconnect_min_seconds",
    "reconnect_max_seconds",
    "stable_session_seconds",
    "max_ws_message_bytes",
    "command_timeout_seconds",
    "stdout_limit",
    "stderr_limit",
    "computer_startup_timeout_seconds",
    "computer_action_timeout_seconds",
    "computer_shutdown_timeout_seconds",
    "computer_max_elements",
)
_AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _environment_option(env: Mapping[str, str], field: str) -> str | None:
    canonical = "RELAY_AGENT_" + field.upper()
    return env.get(canonical)


def _strict_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise ValueError("invalid boolean option")
    return normalized == "true"


def _allow_insecure_ws_from_environment(env: Mapping[str, str]) -> bool:
    raw = env.get("RELAY_ALLOW_INSECURE_WS")
    if raw is None:
        return False
    normalized = raw.strip().lower()
    if normalized not in {"true", "false"}:
        raise ValueError("invalid insecure WebSocket policy")
    return normalized == "true"


def _token_from_environment(env: Mapping[str, str], *, token_key: str) -> str:
    if "RELAY_MCP_TOKEN_FILE" in env or "RELAY_AGENT_TOKEN_FILE" in env:
        raise ConfigurationError()
    token = env.get(token_key)
    if not token:
        raise ConfigurationError()
    return token


def _apply_agent_options(values: dict[str, object], env: Mapping[str, str]) -> None:
    converters: dict[str, Callable[[str], object]] = {
        "heartbeat_interval_seconds": float,
        "reconnect_min_seconds": float,
        "reconnect_max_seconds": float,
        "stable_session_seconds": float,
        "max_ws_message_bytes": int,
        "command_timeout_seconds": float,
        "stdout_limit": int,
        "stderr_limit": int,
        "computer_startup_timeout_seconds": float,
        "computer_action_timeout_seconds": float,
        "computer_shutdown_timeout_seconds": float,
        "computer_max_elements": int,
    }
    for field in _AGENT_OPTION_FIELDS:
        raw = _environment_option(env, field)
        if raw is not None:
            values[field] = converters[field](raw)

    for field in (
        "computer_allowed_app_name",
        "computer_allowed_window_title",
    ):
        raw = _environment_option(env, field)
        if raw is not None:
            values[field] = raw


def _canonical_agent_values(
    env: Mapping[str, str], *, defer_tool_validation: bool = False
) -> dict[str, object]:
    url = env.get("RELAY_URL")
    workspace_value = env.get("RELAY_AGENT_WORKSPACE")
    if not url or not workspace_value:
        raise ConfigurationError()
    token = _token_from_environment(
        env,
        token_key="RELAY_AGENT_TOKEN",
    )
    workspace = _validated_workspace(Path(workspace_value))
    agent_id = _load_or_create_agent_id(workspace, env.get("RELAY_AGENT_ID"))
    tools_allowlist = tuple(
        item.strip()
        for item in env.get("RELAY_AGENT_TOOLS", "").split(",")
        if item.strip()
    )
    values: dict[str, object] = {
        "server_url": url,
        "device_id": agent_id,
        "agent_id": agent_id,
        "agent_token": token,
        "workspace": workspace,
        "allow_insecure_ws": _allow_insecure_ws_from_environment(env),
        "tools_allowlist": tools_allowlist,
    }
    if not defer_tool_validation:
        invalid_tools = [
            item
            for item in tools_allowlist
            if item not in PUBLIC_TO_INTERNAL
            and not re.fullmatch(r"relay_cua_[A-Za-z0-9_]+", item)
        ]
        if invalid_tools:
            raise ValueError("invalid Agent tool allowlist")
    _apply_agent_options(values, env)
    return values


def _validated_workspace(path: Path) -> Path:
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise ValueError("workspace must be an absolute existing non-symlink directory")
    return path.resolve(strict=True)


def _validate_agent_id(value: str) -> str:
    if not _AGENT_ID_PATTERN.fullmatch(value):
        raise ValueError("invalid agent identity")
    return value


def _private_local_path(path: Path, *, directory: bool) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise ValueError("agent identity path must not be a symlink")
    if directory:
        valid_type = stat.S_ISDIR(info.st_mode)
        expected_mode = 0o700
    else:
        valid_type = stat.S_ISREG(info.st_mode)
        expected_mode = 0o600
    if not valid_type or (
        os.name != "nt"
        and (
            stat.S_IMODE(info.st_mode) != expected_mode
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
        )
    ):
        raise ValueError("agent identity path is not private")
    return info


def _ensure_agent_state_dir(workspace: Path) -> Path:
    state_dir = workspace / ".agent-relay"
    created = False
    try:
        state_dir.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    if created:
        try:
            os.chmod(state_dir, 0o700)
        except OSError:
            state_dir.rmdir()
            raise
    _private_local_path(state_dir, directory=True)
    return state_dir


def _read_agent_id_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (
            os.name != "nt"
            and (
                stat.S_IMODE(info.st_mode) != 0o600
                or (hasattr(os, "getuid") and info.st_uid != os.getuid())
            )
        ) or info.st_size > 128:
            raise ValueError("agent identity file is not private")
        value = os.read(fd, 129).decode("utf-8").strip()
    finally:
        os.close(fd)
    return _validate_agent_id(value)


def _create_agent_id_file(path: Path, value: str) -> str:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        existing = _read_agent_id_file(path)
        if existing != value:
            raise ValueError("existing agent identity differs")
        return existing
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        encoded = value.encode("utf-8")
        written = 0
        while written < len(encoded):
            count = os.write(fd, encoded[written:])
            if count <= 0:
                raise OSError("could not persist agent identity")
            written += count
        os.fsync(fd)
    except OSError:
        path.unlink(missing_ok=True)
        raise
    finally:
        os.close(fd)
    return value


def _load_or_create_agent_id(workspace: Path, configured: str | None) -> str:
    selected = _validate_agent_id(configured) if configured else None
    state_dir = _ensure_agent_state_dir(workspace)
    identity_path = state_dir / "agent-id"
    try:
        _private_local_path(identity_path, directory=False)
    except FileNotFoundError:
        if selected is None:
            selected = "agent-" + secrets.token_hex(16)
        return _create_agent_id_file(identity_path, selected)
    existing = _read_agent_id_file(identity_path)
    if selected is not None and selected != existing:
        raise ValueError("existing agent identity differs")
    return existing


def _is_explicit_loopback(hostname: str) -> bool:
    if hostname == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


class TextSocket(Protocol):
    async def send(self, payload: str) -> None: ...
    async def recv(self) -> str: ...


def _connection_options_for(
    settings: AgentSettings,
    connector: Callable[..., Any],
) -> dict[str, Any]:
    options: dict[str, Any] = {
        "max_size": settings.max_ws_message_bytes,
        "proxy": None,
    }
    headers = {"Authorization": "Bearer " + settings.agent_token.get_secret_value()}
    try:
        parameters = inspect.signature(connector).parameters
    except (TypeError, ValueError):
        parameter_names: set[str] = set()
    else:
        parameter_names = set(parameters)
    header_option = (
        "additional_headers"
        if "additional_headers" in parameter_names or "extra_headers" not in parameter_names
        else "extra_headers"
    )
    options[header_option] = headers
    return options


async def check_connection(
    settings: AgentSettings,
    *,
    connector: Callable[..., AsyncContextManager[TextSocket]] | None = None,
) -> None:
    """Verify reachability and authentication with the existing register exchange."""
    connect = connector or websockets.connect
    async with connect(
        settings.server_url,
        **_connection_options_for(settings, connect),
    ) as socket:
        await socket.send(
            json.dumps(
                {"version": 1, "type": "register", "device_id": settings.agent_id},
                separators=(",", ":"),
            )
        )
        raw = await socket.recv()
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > settings.max_ws_message_bytes:
            raise ConnectionError("invalid registration response")
        try:
            registered = parse_server_message(json.loads(raw))
        except (TypeError, ValueError) as exc:
            raise ConnectionError("invalid registration response") from exc
        if not isinstance(registered, Registered) or registered.device_id != settings.agent_id:
            raise ConnectionError("Relay Server rejected registration")


class RelayAgent:
    """One outbound connection; a received cancel can never yield a late result."""

    def __init__(
        self,
        settings: AgentSettings,
        *,
        runner: CommandRunnerProtocol | None = None,
        capabilities: Sequence[LocalCapability] | None = None,
        connector: Callable[..., AsyncContextManager[TextSocket]] | None = None,
        monotonic: Callable[[], float] | None = None,
        catalog: CatalogSnapshot | None = None,
        provider_clients: Mapping[str, ProviderToolClient] | None = None,
    ) -> None:
        self.settings = settings
        supplied_provider_clients = dict(provider_clients or {})
        configured_runner = runner or CommandRunner(settings.workspace, timeout_seconds=settings.command_timeout_seconds, stdout_limit=settings.stdout_limit, stderr_limit=settings.stderr_limit)
        configured_capabilities = (
            [
                SystemCapability(),
                TerminalCapability(configured_runner),
            ]
            if capabilities is None
            else list(capabilities)
        )
        allowed_tools: set[str] | None = None
        selected_catalog: CatalogSnapshot | None = None
        selected_provider_names: set[str] = set()
        if catalog is not None:
            try:
                selected_names = settings.tools_allowlist or ()
                catalog.validate_allowlist(selected_names)
                selected_catalog = catalog.select(selected_names)
            except CatalogError:
                raise ConfigurationError() from None
            allowed_tools = {
                f"{descriptor.provider_name}.{descriptor.tool_name}"
                for descriptor in selected_catalog.selected_descriptors
            }
            selected_provider_names = {
                descriptor.provider_name
                for descriptor in selected_catalog.selected_descriptors
            }
            if (
                capabilities is None
                and "cua" in selected_provider_names
                and "cua" not in supplied_provider_clients
            ):
                cua_provider = _configured_computer_provider(settings)
                if cua_provider is not None:
                    configured_capabilities.append(cast(LocalCapability, cua_provider))
            configured_capabilities = [
                capability
                for capability in configured_capabilities
                if (
                    getattr(capability, "provider_name", None)
                    in selected_provider_names
                    if getattr(capability, "requires_catalog", False)
                    else any(tool in allowed_tools for tool in capability.tools)
                )
            ]
        elif settings.tools_allowlist is not None:
            allowed_tools = {
                PUBLIC_TO_INTERNAL.get(name, name) for name in settings.tools_allowlist
            }
            configured_capabilities = [
                capability
                for capability in configured_capabilities
                if any(tool in allowed_tools for tool in capability.tools)
            ]
        configured_capabilities = [
            capability
            for capability in configured_capabilities
            if not (
                getattr(capability, "requires_catalog", False)
                and selected_catalog is None
            )
        ]
        self._catalog = selected_catalog
        effective_provider_clients = supplied_provider_clients
        if selected_catalog is not None:
            for capability in configured_capabilities:
                provider_name = getattr(capability, "provider_name", None)
                if (
                    provider_name in selected_provider_names
                    and provider_name not in effective_provider_clients
                ):
                    effective_provider_clients[provider_name] = cast(
                        ProviderToolClient, capability
                    )
        self._static_capabilities = self._index_capabilities(
            configured_capabilities, allowed_tools=allowed_tools
        )
        self._capabilities = dict(self._static_capabilities)
        self._unique_capabilities = tuple(dict.fromkeys(map(id, configured_capabilities)))
        self._capability_objects = {id(item): item for item in configured_capabilities}
        self._provider_routes = self._build_provider_routes(
            selected_catalog,
            self._capabilities,
            effective_provider_clients,
            configured_capabilities,
        )
        self._provider_close_objects = {
            id(client): client for client in effective_provider_clients.values()
        }
        if selected_catalog is not None:
            canonical_names = ("system.ping", "terminal.exec")
            canonical_order = {
                name: position for position, name in enumerate(canonical_names)
            }
            selection_order = {
                name: position for position, name in enumerate(settings.tools_allowlist or ())
            }
            selected_entries = tuple(
                entry
                for entry in selected_catalog.entries
                if entry.status == "enabled"
                and f"{entry.descriptor.provider_name}.{entry.descriptor.tool_name}"
                in self._provider_routes
            )
            ordered_entries = sorted(
                enumerate(selected_entries),
                key=lambda item: (
                    0
                    if f"{item[1].descriptor.provider_name}.{item[1].descriptor.tool_name}"
                    in canonical_order
                    else 1,
                    canonical_order.get(
                        f"{item[1].descriptor.provider_name}.{item[1].descriptor.tool_name}",
                        selection_order.get(item[1].public_name, len(selection_order)),
                    ),
                    selection_order.get(item[1].public_name, item[0]),
                ),
            )
            self._announcement_descriptors = tuple(
                entry.descriptor.model_copy(update={"public_name": entry.public_name})
                for _position, entry in ordered_entries
            )
        else:
            self._announcement_descriptors = ()
        self._announcement_tools = (
            tuple(
                f"{descriptor.provider_name}.{descriptor.tool_name}"
                for descriptor in self._announcement_descriptors
            )
            if selected_catalog is not None
            else self._ordered_announcement_tools()
        )
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False
        self._stop_event = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self._connector = connector or websockets.connect
        self._session_registered = False
        self._registered_at: float | None = None
        self._monotonic = monotonic or time.monotonic

    def stop(self) -> None:
        self._stop_event.set()

    def _connection_options(self) -> dict[str, Any]:
        return _connection_options_for(self.settings, self._connector)

    async def run(self) -> None:
        try:
            delay = self.settings.reconnect_min_seconds
            while not self._stop_event.is_set():
                self._session_registered = False
                self._registered_at = None
                connection_open = False
                try:
                    _debug_agent_phase("capabilities-start")
                    await self._start_capabilities()
                    _debug_agent_phase("capabilities-ready")
                    _operator_agent_info(
                        f"connection attempt to {safe_server_target(self.settings.server_url)}"
                    )
                    _debug_agent_phase("connect")
                    async with self._connector(
                        self.settings.server_url,
                        **self._connection_options(),
                    ) as socket:
                        connection_open = True
                        _operator_agent_info("WebSocket connection established")
                        _debug_agent_phase("connected")
                        await self.run_session(socket)
                except asyncio.CancelledError:
                    raise
                except ProviderUnavailableError:
                    _operator_agent_info("local capability became unavailable; stopping")
                    self.stop()
                except Exception as error:
                    if self._session_registered:
                        _operator_agent_info("Relay disconnected; reconnecting")
                    elif connection_open:
                        _operator_agent_info(
                            "registration was rejected or closed before authentication; retrying"
                        )
                    else:
                        _operator_agent_info(
                            "connection or authentication failed; retrying"
                        )
                    phase = getattr(error, "startup_phase", None)
                    detail = f" phase-{phase}" if isinstance(phase, str) else ""
                    _debug_log(f"agent reconnect: {type(error).__name__}{detail}")
                    pass
                else:
                    if self._session_registered and not self._stop_event.is_set():
                        _operator_agent_info("Relay disconnected; reconnecting")
                    elif connection_open and not self._stop_event.is_set():
                        _operator_agent_info(
                            "registration was rejected; retrying"
                        )
                if self._session_was_stable():
                    delay = self.settings.reconnect_min_seconds
                if not self._stop_event.is_set():
                    _operator_agent_info(
                        f"retrying in {delay:g}s (maximum {self.settings.reconnect_max_seconds:g}s)"
                    )
                    await self._sleep_or_stop(delay)
                    delay = min(delay * 2, self.settings.reconnect_max_seconds)
        finally:
            await self.aclose()

    @staticmethod
    def _index_capabilities(
        capabilities: Sequence[LocalCapability],
        *,
        allowed_tools: set[str] | None = None,
    ) -> dict[CapabilityName, LocalCapability]:
        indexed: dict[ToolName, LocalCapability] = {}
        for capability in capabilities:
            if not capability.tools:
                if getattr(capability, "requires_catalog", False):
                    continue
                raise ValueError("unsupported local capability")
            for tool in capability.tools:
                if allowed_tools is not None and tool not in allowed_tools:
                    continue
                if tool not in TOOL_ORDER and not (
                    tool.startswith("cua.") and len(tool) > len("cua.")
                ):
                    raise ValueError("unsupported local capability")
                if tool in indexed:
                    raise ValueError(f"duplicate local capability: {tool}")
                indexed[tool] = capability
        return indexed

    @staticmethod
    def _build_provider_routes(
        catalog: CatalogSnapshot | None,
        capabilities: Mapping[str, LocalCapability],
        provider_clients: Mapping[str, ProviderToolClient],
        capability_objects: Sequence[LocalCapability] = (),
    ) -> dict[str, tuple[ProviderToolClient, ProviderToolDescriptor | None]]:
        routes: dict[str, tuple[ProviderToolClient, ProviderToolDescriptor | None]] = {}
        if catalog is None:
            if provider_clients:
                raise ConfigurationError() from None
            wrappers: dict[int, CapabilityProviderClient] = {}
            builtin_descriptors = {
                f"{descriptor.provider_name}.{descriptor.tool_name}": descriptor
                for descriptor in (
                    *SYSTEM_PROVIDER_DESCRIPTORS,
                    *TERMINAL_PROVIDER_DESCRIPTORS,
                )
            }
            wrapper_descriptors: dict[int, tuple[ProviderToolDescriptor, ...]] = {}
            for wire_name, capability in capabilities.items():
                capability_id = id(capability)
                if capability_id not in wrapper_descriptors:
                    wrapper_descriptors[capability_id] = tuple(
                        descriptor
                        for descriptor in builtin_descriptors.values()
                        if f"{descriptor.provider_name}.{descriptor.tool_name}"
                        in capability.tools
                    )
                wrapper = wrappers.setdefault(
                    capability_id,
                    CapabilityProviderClient(
                        capability, wrapper_descriptors[capability_id]
                    ),
                )
                descriptor = (
                    builtin_descriptors.get(wire_name)
                    if wire_name in builtin_descriptors
                    and wrapper_descriptors[capability_id]
                    else None
                )
                routes[wire_name] = (wrapper, descriptor)
            return routes

        wrapper_descriptors: dict[int, list[ProviderToolDescriptor]] = {}
        wrapper_capabilities: dict[int, LocalCapability] = {}
        dynamic_capabilities = {
            getattr(capability, "provider_name", ""): capability
            for capability in capability_objects
            if getattr(capability, "requires_catalog", False)
        }
        for descriptor in catalog.selected_descriptors:
            wire_name = f"{descriptor.provider_name}.{descriptor.tool_name}"
            if descriptor.provider_name in provider_clients:
                continue
            capability = capabilities.get(wire_name) or dynamic_capabilities.get(
                descriptor.provider_name
            )
            if capability is None:
                continue
            capability_id = id(capability)
            wrapper_capabilities[capability_id] = capability
            wrapper_descriptors.setdefault(capability_id, []).append(descriptor)
        wrappers = {
            capability_id: CapabilityProviderClient(
                capability, wrapper_descriptors[capability_id]
            )
            for capability_id, capability in wrapper_capabilities.items()
        }
        for descriptor in catalog.selected_descriptors:
            wire_name = f"{descriptor.provider_name}.{descriptor.tool_name}"
            provider = provider_clients.get(descriptor.provider_name)
            if provider is not None:
                routes[wire_name] = (provider, descriptor)
            elif wire_name in capabilities:
                capability = capabilities[wire_name]
                routes[wire_name] = (wrappers[id(capability)], descriptor)
        return routes

    async def _start_capabilities(self) -> None:
        for ident in self._unique_capabilities:
            await self._capability_objects[ident].start()
        if self._catalog is None:
            active = dict(self._static_capabilities)
            for ident in self._unique_capabilities:
                capability = self._capability_objects[ident]
                list_tools = getattr(capability, "list_tools", None)
                if not callable(list_tools):
                    continue
                if getattr(capability, "provider_inventory_ready", True) is False:
                    continue
                descriptors = await cast(
                    Callable[[], Coroutine[Any, Any, Sequence[ProviderToolDescriptor]]],
                    list_tools,
                )()
                available = {
                    f"{descriptor.provider_name}.{descriptor.tool_name}"
                    for descriptor in descriptors
                }
                active = {
                    wire_name: owner
                    for wire_name, owner in active.items()
                    if owner is not capability or wire_name in available
                }
            self._capabilities = active
            self._provider_routes = self._build_provider_routes(
                self._catalog,
                self._capabilities,
                {},
            )
            self._announcement_tools = self._ordered_announcement_tools()

    def _ordered_announcement_tools(self) -> tuple[str, ...]:
        ordered = [tool for tool in TOOL_ORDER if tool in self._capabilities]
        ordered.extend(
            tool for tool in self._capabilities if tool not in TOOL_ORDER
        )
        return tuple(ordered)

    async def aclose(self) -> None:
        """Close every configured capability exactly once."""
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._aclose_owned())
        cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                await asyncio.shield(self._close_task)
                break
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
                if self._close_task.done():
                    break
        if cancellation is not None:
            raise cancellation

    async def _aclose_owned(self) -> None:
        for ident in self._unique_capabilities:
            capability = self._capability_objects[ident]
            await asyncio.gather(capability.aclose(), return_exceptions=True)
        for client in self._provider_close_objects.values():
            await asyncio.gather(client.close(), return_exceptions=True)

    def _session_was_stable(self) -> bool:
        """Only reset after a registered connection outlives the local threshold."""
        return (
            self._registered_at is not None
            and self._monotonic() - self._registered_at >= self.settings.stable_session_seconds
        )

    async def _sleep_or_stop(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except TimeoutError:
            pass

    async def run_session(self, socket: TextSocket) -> None:
        _debug_agent_phase("register-send")
        await self._send(
            socket,
            {
                "version": 1,
                "type": "register",
                "device_id": self.settings.agent_id,
            },
        )
        registered = await self._receive(socket)
        if not isinstance(registered, Registered) or registered.device_id != self.settings.agent_id:
            raise ValueError("server did not confirm registration")
        _debug_agent_phase("registered")
        self._session_registered = True
        self._registered_at = self._monotonic()
        _operator_agent_info(
            f"authenticated registration succeeded for agent {self.settings.agent_id}"
        )
        await self._send(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                tools=list(self._announcement_tools),
                descriptors=(
                    list(self._announcement_descriptors)
                    if self._catalog is not None
                    else []
                ),
            ).model_dump(mode="json", exclude_defaults=True),
        )
        capability_summary = ", ".join(self._announcement_tools) or "none"
        _operator_agent_info(
            f"capabilities announced ({len(self._announcement_tools)}): {capability_summary}"
        )
        _debug_agent_phase("capabilities-send")
        heartbeat = asyncio.create_task(self._heartbeat(socket))
        action: asyncio.Task[None] | None = None
        action_request_id: str | None = None
        cancelled_requests: set[str] = set()
        receive: asyncio.Task[object] | None = asyncio.create_task(self._receive(socket))
        stopping = asyncio.create_task(self._stop_event.wait())
        unavailable = {asyncio.create_task(self._capability_objects[ident].wait_unavailable()) for ident in self._unique_capabilities}
        provider_unavailable: set[asyncio.Task[object]] = set()
        for provider in self._provider_close_objects.values():
            waiter = getattr(provider, "wait_unavailable", None)
            if callable(waiter):
                wait_unavailable = cast(Callable[[], Coroutine[Any, Any, None]], waiter)
                provider_unavailable.add(asyncio.create_task(wait_unavailable()))
        try:
            while not self._stop_event.is_set():
                wait_for = {receive, stopping, *unavailable, *provider_unavailable}
                if action is not None:
                    wait_for.add(action)
                done, _ = await asyncio.wait(wait_for, return_when=asyncio.FIRST_COMPLETED)
                if stopping in done:
                    break
                if done & unavailable:
                    if action is not None:
                        if action_request_id is not None:
                            cancelled_requests.add(action_request_id)
                        action.cancel()
                        await asyncio.gather(action, return_exceptions=True)
                        action = None
                        action_request_id = None
                    raise ConnectionError("local capability unavailable")
                if done & provider_unavailable:
                    if action is not None:
                        if action_request_id is not None:
                            cancelled_requests.add(action_request_id)
                        action.cancel()
                        await asyncio.gather(action, return_exceptions=True)
                        action = None
                        action_request_id = None
                    raise ProviderUnavailableError("provider unavailable")
                if action is not None and action in done:
                    await action
                    action = None
                    action_request_id = None
                if receive in done:
                    message = receive.result()
                    receive = asyncio.create_task(self._receive(socket))
                    if isinstance(message, InvokeMessage):
                        if action is not None:
                            await self._send_error(socket, message.request_id, "busy", "an action is already running")
                        else:
                            action = asyncio.create_task(
                                self._perform(socket, message, cancelled_requests)
                            )
                            action_request_id = message.request_id
                    elif isinstance(message, Cancel):
                        if action is not None and action_request_id == message.request_id:
                            cancelled_requests.add(message.request_id)
                            action.cancel()
                            await asyncio.gather(action, return_exceptions=True)
                            cancelled_requests.discard(message.request_id)
                            action = None
                            action_request_id = None
                    else:
                        raise ValueError("unexpected server message")
        finally:
            heartbeat.cancel()
            if receive is not None:
                receive.cancel()
            stopping.cancel()
            for task in unavailable:
                task.cancel()
            for task in provider_unavailable:
                task.cancel()
            if action is not None:
                if action_request_id is not None:
                    cancelled_requests.add(action_request_id)
                action.cancel()
            await asyncio.gather(
                heartbeat,
                stopping,
                *unavailable,
                *provider_unavailable,
                *(item for item in (receive, action) if item is not None),
                return_exceptions=True,
            )

    async def _heartbeat(self, socket: TextSocket) -> None:
        while not self._stop_event.is_set():
            await self._sleep_or_stop(self.settings.heartbeat_interval_seconds)
            if not self._stop_event.is_set():
                await self._send(socket, Heartbeat(version=2, type="heartbeat").model_dump(mode="json"))

    async def _perform(
        self,
        socket: TextSocket,
        message: InvokeMessage,
        cancelled_requests: set[str],
    ) -> None:
        try:
            route = self._provider_routes.get(message.tool_name)
            if route is None:
                raise ValueError("unsupported provider tool")
            provider, descriptor = route
            arguments = message.arguments
            if descriptor is not None:
                arguments = validate_provider_arguments(descriptor, arguments)
                provider_tool_name = descriptor.tool_name
            else:
                provider_tool_name = message.tool_name
            _operator_agent_info(f"Executing tool: {message.tool_name}")
            if isinstance(provider, CapabilityProviderClient):
                result = await provider.call_message(
                    provider_tool_name,
                    arguments,
                    request_id=message.request_id,
                )
            else:
                result = await provider.call_tool(provider_tool_name, arguments)
            if message.request_id in cancelled_requests:
                return
            provider_result = bounded_result(result)
            await self._send(
                socket,
                AgentResult(
                    version=2,
                    type="result",
                    request_id=message.request_id,
                    result=provider_result,
                ).model_dump(mode="json", by_alias=True),
            )
        except asyncio.CancelledError:
            raise
        except CommandFailedError:
            if message.request_id in cancelled_requests:
                return
            await self._send_error(
                socket,
                message.request_id,
                "command_failed",
                "configured command failed",
            )
        except Exception as error:
            if message.request_id in cancelled_requests:
                return
            _debug_log(
                "agent invocation failed: "
                f"tool={message.tool_name} exception={type(error).__name__}"
            )
            await self._send_error(socket, message.request_id, "agent_error", "local action failed")

    async def _receive(self, socket: TextSocket) -> object:
        text = await socket.recv()
        if not isinstance(text, str) or len(text.encode("utf-8")) > self.settings.max_ws_message_bytes:
            raise ValueError("invalid server frame")
        return parse_server_message(json.loads(text))

    async def _send(self, socket: TextSocket, message: object) -> None:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > self.settings.max_ws_message_bytes:
            raise ValueError("outbound message exceeds limit")
        async with self._write_lock:
            await socket.send(payload)

    async def _send_error(self, socket: TextSocket, request_id: str, code: str, message: str) -> None:
        await self._send(
            socket,
            AgentError(
                version=2,
                type="error",
                request_id=request_id,
                error={"code": code, "message": message},
            ).model_dump(mode="json"),
        )

async def _run_with_signal_handlers(agent: RelayAgent) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, agent.stop)
        except NotImplementedError:
            # Windows event loops do not expose add_signal_handler(). The
            # default console handling still interrupts the process safely.
            continue
    await agent.run()


def _runtime_catalog_environment(settings: AgentSettings) -> dict[str, str]:
    """Expose only non-secret provider configuration to catalog discovery."""
    environment: dict[str, str] = {}
    if settings.computer_allowed_app_name is not None:
        environment["RELAY_AGENT_COMPUTER_ALLOWED_APP_NAME"] = (
            settings.computer_allowed_app_name
        )
    if settings.computer_allowed_window_title is not None:
        environment["RELAY_AGENT_COMPUTER_ALLOWED_WINDOW_TITLE"] = (
            settings.computer_allowed_window_title
        )
    environment.update(
        {
            "RELAY_AGENT_COMPUTER_STARTUP_TIMEOUT_SECONDS": str(
                settings.computer_startup_timeout_seconds
            ),
            "RELAY_AGENT_COMPUTER_ACTION_TIMEOUT_SECONDS": str(
                settings.computer_action_timeout_seconds
            ),
            "RELAY_AGENT_COMPUTER_SHUTDOWN_TIMEOUT_SECONDS": str(
                settings.computer_shutdown_timeout_seconds
            ),
        }
    )
    return environment


async def _run_with_runtime_catalog(
    settings: AgentSettings,
    catalog: CatalogSnapshot | None = None,
) -> None:
    """Discover and run with one shared CUA provider lifecycle."""
    def report(phase: str) -> None:
        _debug_log(f"agent runtime phase: {phase}")

    provider = None if catalog is not None else _configured_computer_provider(settings)
    provider_clients: dict[str, ProviderToolClient] = {}
    agent: RelayAgent | None = None
    try:
        if catalog is None:
            report("provider-start")
            if provider is not None:
                try:
                    start = cast(Callable[[], Coroutine[Any, Any, None]], provider.start)  # type: ignore[attr-defined]
                    await start()
                except Exception:
                    await provider.close()
                    provider = None
                else:
                    provider_clients["cua"] = provider
            report("catalog-discovery")
            registrations = local_provider_registrations(
                _runtime_catalog_environment(settings),
                provider_clients,
                allowlist=settings.tools_allowlist,
            )
            catalog = await CatalogService(registrations).discover(
                settings.tools_allowlist or ()
            )
            report("catalog-ready")
        report("agent-construct")
        agent = RelayAgent(
            settings,
            catalog=catalog,
            provider_clients=provider_clients,
        )
        report("agent-run")
        await _run_with_signal_handlers(agent)
    finally:
        if agent is not None:
            await agent.aclose()
        elif provider is not None:
            await provider.close()


def main(
    argv: Sequence[str] | None = None,
    *,
    catalog: CatalogSnapshot | None = None,
) -> None:
    parser = argparse.ArgumentParser(description="Agent Relay outbound agent")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--agent-token", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.agent_token is not None:
        parser.error(
            "--agent-token is unsafe; use RELAY_AGENT_TOKEN, .env, "
            "or the secure onboarding input options"
        )
    try:
        if args.config is not None:
            settings = load_agent_settings(
                args.config,
                catalog=catalog,
                defer_tool_validation=catalog is None,
            )
        else:
            settings = AgentSettings.from_environment(
                defer_tool_validation=catalog is None
            )
    except (ConfigurationError, ValueError):
        parser.error("invalid agent configuration")
    try:
        asyncio.run(_run_with_runtime_catalog(settings, catalog))
    except (ConfigurationError, ValueError):
        parser.error("invalid agent configuration")


if __name__ == "__main__":
    main()

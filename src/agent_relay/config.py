"""Canonical YAML configuration and private dotenv credential primitives."""

from __future__ import annotations

import copy
import os
import secrets
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Mapping
from urllib.parse import unquote_plus, urlparse

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .catalog import (
    CatalogEntry,
    CatalogError,
    CatalogSnapshot,
    validate_agent_allowlist,
)
from .cua_profiles import (
    ALL_PROFILE_PUBLIC_NAMES,
    CuaAccessLevel,
    ReportedCuaAccess,
    cua_access_for_allowlist,
    is_cua_public_name,
    profile_public_names,
)
from .json_bounds import is_sensitive_query_key
from .protocol import TOOL_ORDER

CONFIG_DIR_NAME = ".agent-relay"
DOTENV_FILENAME = ".env"
DOTENV_MAX_BYTES = 4096
DOTENV_KEYS = frozenset({"RELAY_MCP_TOKEN", "RELAY_AGENT_TOKEN"})
DEFAULT_CONFIG_PATH = Path.home() / CONFIG_DIR_NAME / "config.yaml"
PUBLIC_VERSION = "0.1.0"
SERVER_LOCAL_TOOL = "relay_device_status"

PUBLIC_TO_INTERNAL: dict[str, str] = {
    "relay_system_ping": "system.ping",
    "relay_terminal_exec": "terminal.exec",
}
INTERNAL_TO_PUBLIC = {value: key for key, value in PUBLIC_TO_INTERNAL.items()}
PUBLIC_TOOL_NAMES = tuple(PUBLIC_TO_INTERNAL)


def _is_dynamic_cua_public_name(value: object) -> bool:
    return is_cua_public_name(value)


class ConfigError(ValueError):
    """A safe, user-facing configuration error with no secret interpolation."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    internal_name: str | None
    source: str
    description: str
    risk: str = "interaction"


@dataclass(frozen=True)
class ValidationIssue:
    level: Literal["ERROR", "WARNING", "INFO"]
    message: str


@dataclass(frozen=True)
class ValidationReport:
    scope: str
    issues: tuple[ValidationIssue, ...]

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.level == "ERROR")

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.level == "WARNING")

    @property
    def valid(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class CuaToolSummary:
    access: ReportedCuaAccess
    enabled: int
    available: int
    blocked: int
    new_names: tuple[str, ...]


@dataclass(frozen=True)
class ServerRuntime:
    settings: Any
    host: str
    port: int


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


def _set_overlay(document: dict[str, Any], path: str, value: object) -> None:
    current = document
    for part in path.split(".")[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            return
        current = child
    current[path.rsplit(".", 1)[-1]] = value


class ConfigModel(BaseModel):
    """Closed YAML schema with declarative process-environment overlays."""

    model_config = ConfigDict(extra="forbid", validate_default=True)
    env_fields: ClassVar[dict[str, str]] = {}
    csv_env_fields: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def from_sources(cls, raw: Mapping[str, Any], env: Mapping[str, str]):
        values = copy.deepcopy(dict(raw))
        environment_paths: set[str] = set()
        for env_name, path in cls.env_fields.items():
            if env_name in env:
                value: object = env[env_name]
                if env_name in cls.csv_env_fields:
                    value = [item.strip() for item in env[env_name].split(",") if item.strip()]
                _set_overlay(values, path, value)
                environment_paths.add(path)
        return cls.model_validate(values, context={"environment_paths": environment_paths})


class ServerMcpConfig(ConfigModel):
    allowed_hosts: list[str] = Field(default_factory=list)
    allowed_origins: list[str] = Field(default_factory=list)


class ServerRuntimeConfig(ConfigModel):
    min_timeout_seconds: ConfigFloat = Field(default=0.1, ge=0.0001, le=3600)
    max_timeout_seconds: ConfigFloat = Field(default=30.0, ge=0.0001, le=3600)
    cancel_send_timeout_seconds: ConfigFloat = Field(default=0.25, ge=0.0001, le=5)
    max_ws_message_bytes: ConfigInt = Field(default=128 * 1024, ge=1024, le=1024 * 1024)


class ServerConfig(ConfigModel):
    host: str = "127.0.0.1"
    port: ConfigInt = Field(default=8000, ge=1, le=65535)
    mcp: ServerMcpConfig = Field(default_factory=ServerMcpConfig)
    runtime: ServerRuntimeConfig = Field(default_factory=ServerRuntimeConfig)
    env_fields = {
        "RELAY_SERVER_HOST": "host",
        "RELAY_SERVER_PORT": "port",
        "RELAY_MCP_ALLOWED_HOSTS": "mcp.allowed_hosts",
        "RELAY_MCP_ALLOWED_ORIGINS": "mcp.allowed_origins",
        "RELAY_MIN_TIMEOUT_SECONDS": "runtime.min_timeout_seconds",
        "RELAY_MAX_TIMEOUT_SECONDS": "runtime.max_timeout_seconds",
        "RELAY_CANCEL_SEND_TIMEOUT_SECONDS": "runtime.cancel_send_timeout_seconds",
        "RELAY_MAX_WS_MESSAGE_BYTES": "runtime.max_ws_message_bytes",
    }
    csv_env_fields = frozenset({"RELAY_MCP_ALLOWED_HOSTS", "RELAY_MCP_ALLOWED_ORIGINS"})

    @field_validator("host")
    @classmethod
    def valid_host(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("host must not be empty")
        return value

    def runtime_settings(self, *, mcp_token: str, agent_token: str) -> dict[str, Any]:
        return {
            "agent_token": agent_token,
            "mcp_token": mcp_token,
            "bind_host": self.host,
            "mcp_allowed_hosts": tuple(self.mcp.allowed_hosts),
            "mcp_allowed_origins": tuple(self.mcp.allowed_origins),
            **self.runtime.model_dump(),
        }


class AgentIdentityConfig(ConfigModel):
    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._-]+$",
    )

    @field_validator("id")
    @classmethod
    def valid_uuid(cls, value: str, info: ValidationInfo) -> str:
        environment_paths = (info.context or {}).get("environment_paths", set())
        if "identity.id" in environment_paths:
            return value
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
    allowed_app_name: str | None = Field(default=None, min_length=1)
    allowed_window_title: str | None = Field(default=None, min_length=1)
    startup_timeout_seconds: ConfigFloat = Field(default=15.0, gt=0, le=30)
    action_timeout_seconds: ConfigFloat = Field(default=10.0, gt=0, le=30)
    shutdown_timeout_seconds: ConfigFloat = Field(default=3.0, gt=0, le=30)
    max_elements: ConfigInt = Field(default=300, ge=1, le=1000)

    @model_validator(mode="after")
    def complete_policy(self) -> AgentComputerConfig:
        if (self.allowed_app_name is None) != (self.allowed_window_title is None):
            raise ValueError("computer policy fields must be provided together")
        return self


class AgentRuntimeConfig(ConfigModel):
    heartbeat_interval_seconds: ConfigFloat = Field(default=15.0, ge=0.0001, le=3600)
    reconnect_min_seconds: ConfigFloat = Field(default=0.1, ge=0.0001, le=60)
    reconnect_max_seconds: ConfigFloat = Field(default=5.0, ge=0.0001, le=3600)
    stable_session_seconds: ConfigFloat = Field(default=30.0, ge=1, le=3600)
    max_ws_message_bytes: ConfigInt = Field(default=128 * 1024, ge=1024, le=1024 * 1024)
    command_timeout_seconds: ConfigFloat = Field(default=30.0, ge=0.0001, le=3600)
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
        "RELAY_URL": "relay_url",
        "RELAY_AGENT_ID": "identity.id",
        "RELAY_AGENT_WORKSPACE": "workspace",
        "RELAY_AGENT_TOOLS": "tools.allowlist",
        **{
            f"RELAY_AGENT_{field.upper()}": f"runtime.{field}"
            for field in AgentRuntimeConfig.model_fields
        },
        **{
            f"RELAY_AGENT_COMPUTER_{field.upper()}": f"computer.{field}"
            for field in AgentComputerConfig.model_fields
        },
    }
    csv_env_fields = frozenset({"RELAY_AGENT_TOOLS"})

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

    def runtime_settings(self, *, token: str, config_path: Path) -> dict[str, Any]:
        identity = self.identity.id
        return {
            "server_url": self.relay_url,
            "device_id": identity,
            "agent_id": identity,
            "agent_token": token,
            "workspace": _relative_path(self.workspace, config_path),
            "tools_allowlist": tuple(self.tools.allowlist),
            **self.runtime.model_dump(),
            **{f"computer_{name}": value for name, value in self.computer.model_dump().items()},
        }


def configuration_keys(model: type[ConfigModel]) -> frozenset[str]:
    """Derive dotted CLI keys from the canonical model tree."""
    keys: set[str] = set()

    def walk(current: type[ConfigModel], prefix: str = "") -> None:
        for name, field in current.model_fields.items():
            path = f"{prefix}.{name}" if prefix else name
            if isinstance(field.annotation, type) and issubclass(field.annotation, ConfigModel):
                walk(field.annotation, path)
            else:
                keys.add(path)

    walk(model)
    return frozenset(keys)


_CONFIG_MODELS: dict[str, type[ConfigModel]] = {
    "server": ServerConfig,
    "agent": AgentConfig,
}
_CONFIG_KEYS = {scope: configuration_keys(model) for scope, model in _CONFIG_MODELS.items()}


def _tool_specs() -> tuple[ToolSpec, ...]:
    descriptions = {
        "system.ping": "fixed local health check",
        "terminal.exec": "fixed allowlisted terminal command",
    }
    internal_names = TOOL_ORDER
    specs = [
        ToolSpec(
            name=INTERNAL_TO_PUBLIC[internal],
            internal_name=internal,
            source="builtin",
            description=descriptions[internal],
        )
        for internal in internal_names
    ]
    return tuple(specs) + (
        ToolSpec(
            name=SERVER_LOCAL_TOOL,
            internal_name=None,
            source="server",
            description="server-local Relay connection status",
        ),
    )


TOOL_SPECS = _tool_specs()


def _deep_merge(base: dict[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in update.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _default_document(scope: Literal["server", "agent"]) -> dict[str, Any]:
    return _CONFIG_MODELS[scope]().model_dump(mode="json")


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        if os.name != "nt":
            raise
    _check_private_path(path, directory=True)


def _check_private_path(path: Path, *, directory: bool) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise ConfigError("configuration path must not be a symlink")
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(info.st_mode):
        raise ConfigError("configuration path has an invalid type")
    if os.name != "nt":
        if stat.S_IMODE(info.st_mode) not in ({0o700} if directory else {0o600}):
            raise ConfigError("configuration path is not private")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ConfigError("configuration path has an invalid owner")


def _check_parent_chain(path: Path) -> None:
    current = path.parent
    while True:
        if current.exists() or current.is_symlink():
            try:
                if stat.S_ISLNK(current.lstat().st_mode):
                    raise ConfigError("configuration parent path must not be a symlink")
            except FileNotFoundError:
                pass
        if current.parent == current:
            return
        current = current.parent


def _assert_no_symlink(path: Path) -> None:
    _check_parent_chain(path)
    if path.is_symlink():
        raise ConfigError("configuration path must not be a symlink")


def _write_private_text(path: Path, content: str, *, overwrite: bool = True) -> None:
    _check_parent_chain(path)
    _ensure_private_directory(path.parent)
    if path.exists() or path.is_symlink():
        _check_private_path(path, directory=False)
        if not overwrite:
            raise ConfigError("refusing to overwrite an existing secret file")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content.rstrip("\r\n") + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            if os.name != "nt":
                raise
        _check_private_path(path, directory=False)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _read_private_text(path: Path) -> str:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ConfigError("required secret file is unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise ConfigError("required secret file is invalid")
        if os.name != "nt" and (
            stat.S_IMODE(info.st_mode) != 0o600
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
        ):
            raise ConfigError("required secret file is not private")
        value = os.read(fd, 4097).decode("utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError("required secret file is invalid") from exc
    finally:
        os.close(fd)
    if not value:
        raise ConfigError("required secret file is empty")
    return value


def dotenv_path(path: str | Path | None) -> Path:
    """Return the private credential file next to the selected YAML file."""
    return _config_path(path).parent / DOTENV_FILENAME


def _read_dotenv(path: Path) -> dict[str, str]:
    """Read the deliberately small, non-expanding Agent Relay credential file."""
    if not path.exists() and not path.is_symlink():
        return {}
    _assert_no_symlink(path)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ConfigError(".env file is unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(".env file is not a private regular file")
        if info.st_size > DOTENV_MAX_BYTES:
            raise ConfigError(".env file is too large")
        if os.name != "nt" and (
            stat.S_IMODE(info.st_mode) != 0o600
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
        ):
            raise ConfigError(".env file is not private")
        raw = os.read(fd, DOTENV_MAX_BYTES + 1)
    except OSError as exc:
        raise ConfigError(".env file could not be read") from exc
    finally:
        os.close(fd)
    if len(raw) > DOTENV_MAX_BYTES:
        raise ConfigError(".env file is too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(".env file is not valid UTF-8") from exc
    values: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "\x00" in line or "=" not in line:
            raise ConfigError(f".env line {line_number} is invalid")
        raw_key, raw_value = line.split("=", 1)
        key = raw_key.strip()
        value = raw_value.strip()
        if raw_key != key or key not in DOTENV_KEYS:
            raise ConfigError(f".env key is not allowed: {key or '<empty>'}")
        if key in values:
            raise ConfigError(f".env key is duplicated: {key}")
        if not value:
            raise ConfigError(f".env value is empty: {key}")
        values[key] = value
    return values


def _reject_legacy_secret_sections(document: Mapping[str, Any]) -> None:
    for scope in ("server", "agent"):
        section = document.get(scope)
        if isinstance(section, Mapping) and "secrets" in section:
            raise ConfigError(
                "legacy secrets configuration is unsupported; create .env next to the YAML file"
            )


def _write_dotenv(path: Path, values: Mapping[str, str]) -> None:
    unknown = set(values) - DOTENV_KEYS
    if unknown:
        raise ConfigError(".env contains an unsupported secret key")
    for key, value in values.items():
        if not isinstance(value, str) or not value or any(
            character in value for character in "\r\n\x00"
        ):
            raise ConfigError(f".env value is invalid: {key}")
    content = "".join(
        f"{key}={values[key]}\n"
        for key in ("RELAY_MCP_TOKEN", "RELAY_AGENT_TOKEN")
        if key in values
    )
    _write_private_text(path, content)


def _update_dotenv(path: Path, key: str, value: str | None) -> None:
    if key not in DOTENV_KEYS:
        raise ConfigError("unsupported .env secret key")
    values = _read_dotenv(path)
    if value is None:
        values.pop(key, None)
    else:
        if not value:
            raise ConfigError("secret cannot be empty")
        values[key] = value
    _write_dotenv(path, values)


def has_token_source(
    path: str | Path | None, key: Literal["RELAY_MCP_TOKEN", "RELAY_AGENT_TOKEN"],
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Report whether a non-empty process or dotenv token is available."""
    effective_env = os.environ if env is None else env
    if key in effective_env:
        return bool(effective_env[key])
    try:
        return key in _read_dotenv(dotenv_path(path))
    except ConfigError:
        return False


def _token_source_present(
    path: str | Path | None,
    key: Literal["RELAY_MCP_TOKEN", "RELAY_AGENT_TOKEN"],
    *,
    env: Mapping[str, str],
) -> bool:
    """Report whether a source exists, including an explicitly empty env value."""
    return key in env or has_token_source(path, key, env=env)


def _write_config(path: Path, document: Mapping[str, Any]) -> None:
    _check_parent_chain(path)
    _ensure_private_directory(path.parent)
    if path.exists() or path.is_symlink():
        _check_private_path(path, directory=False)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            yaml.safe_dump(
                _serializable(document),
                stream,
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        _check_private_path(path, directory=False)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _serializable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable(item) for item in value]
    return value


def _load_yaml(path: Path, *, required: bool = True) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigError(f"configuration file does not exist: {path}")
        return {}
    _check_parent_chain(path)
    _check_private_path(path, directory=False)
    try:
        with path.open("r", encoding="utf-8") as stream:
            loaded = yaml.safe_load(stream)
    except ConfigError:
        raise
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError("configuration file could not be read") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError("configuration root must be a mapping")
    return loaded


def _config_path(path: str | Path | None) -> Path:
    return (DEFAULT_CONFIG_PATH if path is None else Path(path)).expanduser()


def _relative_path(value: object, config_path: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("configuration path must be a non-empty string")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    _assert_no_symlink(path)
    return path.resolve(strict=False)


def _reject_token_file_environment(env: Mapping[str, str]) -> None:
    if "RELAY_MCP_TOKEN_FILE" in env or "RELAY_AGENT_TOKEN_FILE" in env:
        raise ConfigError("*_TOKEN_FILE is no longer supported; use .env")

def _effective_server(document: Mapping[str, Any], env: Mapping[str, str]) -> ServerConfig:
    raw = document.get("server", {})
    if not isinstance(raw, Mapping):
        raise ConfigError("server configuration must be a mapping")
    try:
        return ServerConfig.from_sources(raw, env)
    except ValidationError as exc:
        raise ConfigError("server configuration is invalid") from exc


def _effective_agent(document: Mapping[str, Any], env: Mapping[str, str]) -> AgentConfig:
    raw = document.get("agent", {})
    if not isinstance(raw, Mapping):
        raise ConfigError("agent configuration must be a mapping")
    try:
        return AgentConfig.from_sources(raw, env)
    except ValidationError as exc:
        raise ConfigError("agent configuration is invalid") from exc


def catalog_environment(
    path: str | Path | None,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Return deterministic catalog inputs from the effective Agent settings."""
    config_path = _config_path(path)
    effective_env = os.environ if env is None else env
    _reject_token_file_environment(effective_env)
    document = _load_yaml(config_path, required=False)
    _reject_legacy_secret_sections(document)
    agent = _effective_agent(document, effective_env)
    catalog_env = dict(effective_env)

    for field in (
        "allowed_app_name",
        "allowed_window_title",
        "startup_timeout_seconds",
        "action_timeout_seconds",
        "shutdown_timeout_seconds",
    ):
        value = getattr(agent.computer, field)
        if value is not None:
            catalog_env.setdefault(f"RELAY_AGENT_COMPUTER_{field.upper()}", str(value))
    return catalog_env, list(agent.tools.allowlist)


def _secret_value(
    document: Mapping[str, Any],
    scope: Literal["server", "agent"],
    name: Literal["mcp", "agent"],
    path: Path,
    env: Mapping[str, str],
) -> str:
    env_key = "RELAY_MCP_TOKEN" if name == "mcp" else "RELAY_AGENT_TOKEN"
    env_file_key = (
        "RELAY_MCP_TOKEN_FILE" if name == "mcp" else "RELAY_AGENT_TOKEN_FILE"
    )
    if env_file_key in env:
        raise ConfigError(f"{env_file_key} is no longer supported; use .env")
    if env_key in env:
        value = env[env_key]
        if not value:
            raise ConfigError("required token is empty")
        return value
    values = _read_dotenv(path.parent / DOTENV_FILENAME)
    try:
        return values[env_key]
    except KeyError as exc:
        raise ConfigError(
            f"required token is unavailable; set {env_key} or create {DOTENV_FILENAME}"
        ) from exc


def read_private_secret(path: str | Path) -> str:
    """Read one operator-supplied private secret file safely."""
    secret_path = Path(path).expanduser()
    _assert_no_symlink(secret_path)
    return _read_private_text(secret_path)


def read_server_agent_token(
    path: str | Path | None,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Read the effective Server-to-Agent credential without exposing its path."""
    config_path = _config_path(path)
    document = _load_yaml(config_path)
    _reject_legacy_secret_sections(document)
    if "server" not in document:
        raise ConfigError("server configuration is not initialized")
    if not isinstance(document["server"], Mapping):
        raise ConfigError("server configuration must be a mapping")
    effective_env = os.environ if env is None else env
    _reject_token_file_environment(effective_env)
    return _secret_value(document, "server", "agent", config_path, effective_env)


def validate_relay_url(value: str) -> None:
    """Validate a Relay WebSocket URL without exposing user input in errors."""
    try:
        AgentConfig.model_validate({"relay_url": value})
    except ValidationError as exc:
        raise ConfigError("agent relay_url must be a ws:// or wss:// URL") from exc


def validate_agent_transport(value: str) -> None:
    """Validate the structural Relay WebSocket URL contract."""
    validate_relay_url(value)


def _validate_root(document: Mapping[str, Any], report: list[ValidationIssue]) -> None:
    allowed = {"server", "agent"}
    unknown = sorted(set(document) - allowed)
    if unknown:
        report.append(ValidationIssue("ERROR", f"unknown root key(s): {', '.join(unknown)}"))


def _model_validation_issues(
    error: ValidationError, scope: Literal["server", "agent"]
) -> list[ValidationIssue]:
    """Translate canonical model failures to stable, sanitized CLI messages."""
    messages: list[str] = []
    for detail in error.errors(include_url=False, include_input=False):
        path = ".".join(str(part) for part in detail["loc"])
        error_type = str(detail["type"])
        if error_type == "extra_forbidden":
            message = (
                "legacy secrets configuration is unsupported; create .env next to the YAML file"
                if path == "secrets" or path.startswith("secrets.")
                else f"unknown {scope} configuration key: {path}"
            )
        elif scope == "server":
            message = (
                "server host is invalid"
                if path == "host"
                else "server port is invalid"
                if path == "port"
                else "server runtime limits are invalid"
                if path.startswith("runtime")
                else f"server configuration field is invalid: {path}"
            )
        elif path.startswith("identity"):
            message = "agent identity.id must be a UUID"
        elif path == "relay_url":
            message = "agent relay_url must be a ws:// or wss:// URL"
        elif path == "workspace":
            message = "workspace is invalid (configuration path must be a non-empty string)"
        elif path == "tools" or path.startswith("tools.allowlist"):
            message = (
                "tools.allowlist contains duplicates"
                if "duplicates" in str(detail["msg"])
                else "tools.allowlist must be a list of tool names"
            )
        elif path.startswith("computer"):
            message = (
                "computer section must be a mapping"
                if path == "computer" and error_type == "model_type"
                else "computer configuration must provide all policy fields"
                if path == "computer" and error_type == "value_error"
                else "computer configuration is invalid"
            )
        elif path.startswith("runtime"):
            message = "agent runtime limits are invalid"
        else:
            message = f"agent configuration field is invalid: {path}"
        if message not in messages:
            messages.append(message)
    return [ValidationIssue("ERROR", message) for message in messages]


def _validate_server(
    document: Mapping[str, Any], path: Path, env: Mapping[str, str], *, require: bool
) -> ValidationReport:
    issues: list[ValidationIssue] = []
    _validate_root(document, issues)
    raw = document.get("server")
    if raw is None:
        issues.append(
            ValidationIssue(
                "ERROR" if require else "INFO",
                "server section is missing" if require else "server section is not configured",
            )
        )
        return ValidationReport("server", tuple(issues))
    if not isinstance(raw, Mapping):
        return ValidationReport(
            "server", (ValidationIssue("ERROR", "server section must be a mapping"),)
        )
    try:
        section = ServerConfig.from_sources(raw, env)
    except ValidationError as exc:
        section = None
        issues.extend(_model_validation_issues(exc, "server"))
    if section is not None:
        issues.append(ValidationIssue("INFO", f"port={section.port}"))
    token_values: dict[str, str] = {}
    dotenv_error: str | None = None
    try:
        dotenv_values = _read_dotenv(path.parent / DOTENV_FILENAME)
    except ConfigError as exc:
        dotenv_values = {}
        dotenv_error = str(exc)
    for name in ("mcp", "agent"):
        env_key = "RELAY_MCP_TOKEN" if name == "mcp" else "RELAY_AGENT_TOKEN"
        env_file_key = (
            "RELAY_MCP_TOKEN_FILE" if name == "mcp" else "RELAY_AGENT_TOKEN_FILE"
        )
        if env_file_key in env:
            issues.append(
                ValidationIssue(
                    "ERROR", f"{env_file_key} is no longer supported; use .env"
                )
            )
        elif env_key in env:
            if not env[env_key]:
                issues.append(ValidationIssue("ERROR", f"{name} token is empty"))
            else:
                issues.append(
                    ValidationIssue("INFO", f"{name}_token source=environment")
                )
                token_values[name] = env[env_key]
        elif dotenv_error is not None:
            issues.append(
                ValidationIssue(
                    "ERROR", f"{name} token is unavailable ({dotenv_error})"
                )
            )
        elif env_key in dotenv_values:
            issues.append(ValidationIssue("INFO", f"{name}_token source=.env"))
            token_values[name] = dotenv_values[env_key]
        else:
            issues.append(
                ValidationIssue(
                    "ERROR",
                    f"{name} token is unavailable; set {env_key} or create .env",
                )
            )
    if len(token_values) == 2 and token_values["mcp"] == token_values["agent"]:
        issues.append(ValidationIssue("ERROR", "mcp and agent tokens must be distinct"))
    return ValidationReport("server", tuple(issues))


def _validate_agent(
    document: Mapping[str, Any],
    path: Path,
    env: Mapping[str, str],
    *,
    require: bool,
    catalog: CatalogSnapshot | None = None,
    defer_tool_validation: bool = False,
) -> ValidationReport:
    issues: list[ValidationIssue] = []
    _validate_root(document, issues)
    raw = document.get("agent")
    if raw is None:
        issues.append(
            ValidationIssue(
                "ERROR" if require else "INFO",
                "agent section is missing" if require else "agent section is not configured",
            )
        )
        return ValidationReport("agent", tuple(issues))
    if not isinstance(raw, Mapping):
        return ValidationReport(
            "agent", (ValidationIssue("ERROR", "agent section must be a mapping"),)
        )
    try:
        section = AgentConfig.from_sources(raw, env)
    except ValidationError as exc:
        section = None
        issues.extend(_model_validation_issues(exc, "agent"))

    if section is not None:
        parsed = urlparse(section.relay_url)
        issues.append(
            ValidationIssue(
                "INFO",
                "transport=ws:// (unencrypted; intended for local or trusted LAN use)"
                if parsed.scheme == "ws"
                else "transport=wss:// (TLS expected)",
            )
        )
        try:
            workspace = _relative_path(section.workspace, path)
            if workspace.is_symlink() or not workspace.is_dir():
                raise ConfigError("workspace must be an existing directory")
            issues.append(ValidationIssue("INFO", f"workspace={workspace}"))
        except ConfigError as exc:
            issues.append(ValidationIssue("ERROR", f"workspace is invalid ({exc})"))

    try:
        _reject_token_file_environment(env)
        _secret_value(document, "agent", "agent", path, env)
        source = "environment" if "RELAY_AGENT_TOKEN" in env else ".env"
        issues.append(ValidationIssue("INFO", f"agent_token source={source}"))
    except ConfigError as exc:
        issues.append(ValidationIssue("ERROR", f"agent token is unavailable ({exc})"))

    if section is not None:
        try:
            validate_agent_allowlist(
                section.tools.allowlist,
                catalog=catalog,
                defer_unknown=defer_tool_validation,
            )
        except CatalogError as exc:
            issues.append(ValidationIssue("ERROR", str(exc)))
        if not section.tools.allowlist:
            issues.append(ValidationIssue("INFO", "no tools enabled"))
    return ValidationReport("agent", tuple(issues))


def validate_document(
    path: str | Path | None,
    scope: Literal["server", "agent"],
    *,
    env: Mapping[str, str] | None = None,
    require: bool = True,
    catalog: CatalogSnapshot | None = None,
) -> ValidationReport:
    config_path = _config_path(path)
    effective_env = os.environ if env is None else env
    _reject_token_file_environment(effective_env)
    try:
        document = _load_yaml(config_path)
    except ConfigError as exc:
        if require:
            raise
        if scope == "server" and _token_source_present(path, "RELAY_MCP_TOKEN", env=effective_env) and _token_source_present(path, "RELAY_AGENT_TOKEN", env=effective_env):
            document = {"server": _default_document("server")}
        elif scope == "agent" and effective_env.get("RELAY_URL") and effective_env.get("RELAY_AGENT_WORKSPACE") and _token_source_present(path, "RELAY_AGENT_TOKEN", env=effective_env):
            document = {"agent": _default_document("agent")}
        else:
            return ValidationReport(scope, (ValidationIssue("ERROR", str(exc)),))
    if scope == "server":
        return _validate_server(document, config_path, effective_env, require=require)
    return _validate_agent(
        document,
        config_path,
        effective_env,
        require=require,
        catalog=catalog,
    )


def _invalid_configuration_message(scope: str, report: ValidationReport) -> str:
    """Render only sanitized validation errors for runtime startup failures."""
    details = "; ".join(issue.message for issue in report.errors)
    prefix = f"invalid {scope} configuration"
    return f"{prefix}: {details}" if details else prefix


def _existing_allowlist(section: Mapping[str, Any]) -> list[str]:
    tools = section.get("tools", {})
    if not isinstance(tools, Mapping):
        raise ConfigError("agent tools must be a mapping")
    allowlist = tools.get("allowlist", [])
    if not isinstance(allowlist, list):
        raise ConfigError("tools.allowlist must be a list of tool names")
    try:
        return list(validate_agent_allowlist(allowlist, defer_unknown=True))
    except CatalogError as exc:
        raise ConfigError(str(exc)) from None


def _validate_cua_profile_catalog(
    level: CuaAccessLevel,
    catalog: CatalogSnapshot | None,
) -> tuple[str, ...]:
    """Validate an explicit profile against the current CUA inventory."""
    try:
        names = profile_public_names(level)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    if level == "none":
        return names
    if catalog is None:
        raise ConfigError("CUA catalog is unavailable; cannot apply the requested profile")
    entries = {
        entry.public_name: entry
        for entry in catalog.entries
        if entry.provider_name == "cua"
    }
    missing = [name for name in names if name not in entries]
    if missing:
        raise ConfigError(
            f"CUA {level} profile is not available in the current catalog: {missing[0]}"
        )
    unavailable = [name for name in names if entries[name].status == "unavailable"]
    if unavailable:
        raise ConfigError(
            f"CUA {level} profile contains unavailable tool: {unavailable[0]}"
        )
    # The profile is versioned and explicit, but policy remains authoritative:
    # a policy-blocked descriptor is omitted from the resulting allowlist even
    # when the maintained profile contains its name.
    names = tuple(name for name in names if entries[name].status != "blocked")
    try:
        catalog.validate_allowlist(names)
    except CatalogError as exc:
        raise ConfigError(str(exc)) from None
    return names


def validate_cua_profile(
    level: str,
    catalog: CatalogSnapshot | None,
) -> tuple[str, ...]:
    """Validate a CLI/onboarding CUA profile and return public names."""
    if level not in {"none", "standard", "full"}:
        raise ConfigError(f"unknown CUA access level: {level}")
    return _validate_cua_profile_catalog(level, catalog)  # type: ignore[arg-type]


def apply_cua_access(
    allowlist: list[str] | tuple[str, ...],
    level: str,
    catalog: CatalogSnapshot | None,
) -> list[str]:
    """Return a stable allowlist after atomically replacing only its CUA part."""
    names = validate_cua_profile(level, catalog)
    non_cua = [name for name in allowlist if not _is_dynamic_cua_public_name(name)]
    result = non_cua + list(names)
    try:
        validate_agent_allowlist(result, catalog=catalog)
    except CatalogError as exc:
        raise ConfigError(str(exc)) from None
    return result


def update_cua_access(
    path: str | Path | None,
    level: str,
    *,
    catalog: CatalogSnapshot | None = None,
) -> bool:
    """Apply a CUA profile in one YAML write; return False only on no-op."""
    config_path = _config_path(path)
    document = _load_yaml(config_path)
    _reject_legacy_secret_sections(document)
    section = document.get("agent")
    if not isinstance(section, Mapping):
        raise ConfigError("agent configuration is not initialized")
    current = _existing_allowlist(section)
    updated = apply_cua_access(current, level, catalog)
    if updated == current:
        return True
    agent_section = copy.deepcopy(dict(section))
    tools = dict(agent_section.get("tools", {}))
    tools["allowlist"] = updated
    agent_section["tools"] = tools
    document["agent"] = agent_section
    _write_config(config_path, document)
    return True


def init_config(
    path: str | Path | None,
    scope: Literal["server", "agent"],
    *,
    force: bool = False,
    token: str | None = None,
    tools: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    catalog: CatalogSnapshot | None = None,
    relay_url: str | None = None,
    workspace: str | Path | None = None,
    cua_access: str | None = None,
) -> Path:
    config_path = _config_path(path)
    effective_env = os.environ if env is None else env
    _reject_token_file_environment(effective_env)
    if scope == "server" and cua_access is not None:
        raise ConfigError("CUA access is only valid for Agent configuration")
    document = _load_yaml(config_path, required=False)
    _reject_legacy_secret_sections(document)
    dotenv_file = config_path.parent / DOTENV_FILENAME
    dotenv_values = _read_dotenv(dotenv_file)
    defaults = _default_document(scope)
    existing = document.get(scope)
    if force and isinstance(existing, Mapping):
        preserved = {
            key: copy.deepcopy(existing[key])
            for key in (
                () if scope == "server" else ("identity", "tools")
            )
            if key in existing
        }
        section = _deep_merge(defaults, preserved)
    else:
        section = _deep_merge(defaults, existing if isinstance(existing, Mapping) else {})
    if scope == "server":
        for name in ("mcp", "agent"):
            env_token = "RELAY_MCP_TOKEN" if name == "mcp" else "RELAY_AGENT_TOKEN"
            env_file_key = (
                "RELAY_MCP_TOKEN_FILE"
                if name == "mcp"
                else "RELAY_AGENT_TOKEN_FILE"
            )
            if env_file_key in effective_env:
                raise ConfigError(f"{env_file_key} is no longer supported; use .env")
            if env_token in effective_env and not effective_env[env_token]:
                raise ConfigError(f"{name} token is empty")
        generated: dict[str, str] = {}
        for name in ("mcp", "agent"):
            env_token = "RELAY_MCP_TOKEN" if name == "mcp" else "RELAY_AGENT_TOKEN"
            if env_token not in effective_env and env_token not in dotenv_values:
                generated[env_token] = secrets.token_urlsafe(32)
        effective_tokens = {
            key: effective_env.get(key, dotenv_values.get(key, generated.get(key)))
            for key in DOTENV_KEYS
        }
        if (
            effective_tokens["RELAY_MCP_TOKEN"] is not None
            and effective_tokens["RELAY_MCP_TOKEN"]
            == effective_tokens["RELAY_AGENT_TOKEN"]
        ):
            raise ConfigError("mcp and agent tokens must be distinct")
        document["server"] = section
        _write_config(config_path, document)
        if generated:
            dotenv_values.update(generated)
            _write_dotenv(dotenv_file, dotenv_values)
    else:
        if relay_url is not None:
            section["relay_url"] = relay_url
        if workspace is not None:
            section["workspace"] = str(workspace)
        _reject_token_file_environment(effective_env)
        if "RELAY_AGENT_TOKEN" in effective_env and not effective_env["RELAY_AGENT_TOKEN"]:
            raise ConfigError("agent token is empty")
        if tools is not None:
            requested_tools = list(tools)
            if cua_access is not None and any(
                _is_dynamic_cua_public_name(name) for name in requested_tools
            ):
                raise ConfigError(
                    "--tools cannot include relay_cua_* when --cua-access is used"
                )
        elif existing is None:
            requested_tools = []
        else:
            requested_tools = _existing_allowlist(section)

        if cua_access is not None:
            requested_tools = apply_cua_access(
                requested_tools,
                cua_access,
                catalog,
            )
        try:
            validate_agent_allowlist(requested_tools, catalog=catalog)
        except CatalogError as exc:
            raise ConfigError(str(exc)) from None
        section.setdefault("tools", {})["allowlist"] = requested_tools
        workspace = _relative_path(section["workspace"], config_path)
        _ensure_private_directory(workspace)
        section["workspace"] = _relative_config_value(section["workspace"], config_path)
        document["agent"] = section
        _write_config(config_path, document)
        if (
            token is None
            and existing is not None
            and "RELAY_AGENT_TOKEN" not in effective_env
        ):
            token = dotenv_values.get("RELAY_AGENT_TOKEN")
        if token is None and "RELAY_AGENT_TOKEN" not in effective_env:
            raise ConfigError("agent token is required")
        if token is not None and not token:
            raise ConfigError("agent token cannot be empty")
        if token is not None and "RELAY_AGENT_TOKEN" not in effective_env:
            _update_dotenv(dotenv_file, "RELAY_AGENT_TOKEN", token)
    return config_path


def _relative_config_value(value: object, path: Path) -> str:
    if isinstance(value, str) and not Path(value).expanduser().is_absolute():
        return value
    return str(_relative_path(value, path))


def _tool_is_available(spec: ToolSpec, document: Mapping[str, Any], path: Path) -> bool:
    """Report availability for the fixed in-process tools."""
    del document, path
    return spec.internal_name in {"system.ping", "terminal.exec"}


def _tool_spec_from_catalog(entry: CatalogEntry) -> ToolSpec:
    return ToolSpec(
        name=entry.public_name,
        internal_name=entry.tool_name,
        source=entry.source,
        description=entry.description,
        risk=entry.risk,
    )


def tool_statuses(
    path: str | Path | None,
    *,
    env: Mapping[str, str] | None = None,
    catalog: CatalogSnapshot | None = None,
) -> list[tuple[ToolSpec, str]]:
    config_path = _config_path(path)
    effective_env = os.environ if env is None else env
    _reject_token_file_environment(effective_env)
    try:
        document = _load_yaml(config_path)
    except ConfigError:
        if not (effective_env.get("RELAY_URL") and effective_env.get("RELAY_AGENT_WORKSPACE")):
            raise
        document = {"agent": _default_document("agent")}
    _reject_legacy_secret_sections(document)
    agent = _effective_agent(document, effective_env)
    allowlist = list(agent.tools.allowlist)
    if catalog is not None:
        try:
            selected = catalog.select(allowlist)
        except CatalogError as exc:
            raise ConfigError(str(exc)) from None
        return [
            *(
                (_tool_spec_from_catalog(entry), entry.status)
                for entry in selected.entries
            ),
            (
                ToolSpec(
                    name=SERVER_LOCAL_TOOL,
                    internal_name=None,
                    source="server",
                    description="server-local Relay connection status",
                    risk="read_only",
                ),
                "server-local",
            ),
        ]

    effective_document = copy.deepcopy(document)
    effective_document["agent"] = agent.model_dump(mode="json")
    statuses: list[tuple[ToolSpec, str]] = []
    for spec in TOOL_SPECS:
        if spec.source == "server":
            statuses.append((spec, "server-local"))
        elif not _tool_is_available(spec, effective_document, config_path):
            statuses.append((spec, "unavailable"))
        elif spec.name in set(allowlist):
            statuses.append((spec, "enabled"))
        else:
            statuses.append((spec, "disabled"))
    return statuses


def update_tool(
    path: str | Path | None,
    name: str,
    *,
    enabled: bool,
    catalog: CatalogSnapshot | None = None,
) -> None:
    if name == SERVER_LOCAL_TOOL:
        raise ConfigError("relay_device_status is server-local and cannot be configured")
    if catalog is not None and not enabled:
        try:
            catalog.entry(name)
        except CatalogError as exc:
            raise ConfigError(str(exc)) from None
    else:
        try:
            validate_agent_allowlist([name], catalog=catalog)
        except CatalogError as exc:
            raise ConfigError(str(exc)) from None
    config_path = _config_path(path)
    document = _load_yaml(config_path)
    _reject_legacy_secret_sections(document)
    if "agent" not in document or not isinstance(document["agent"], Mapping):
        raise ConfigError("agent configuration is not initialized")
    if (
        catalog is None
        and name in PUBLIC_TO_INTERNAL
        and not _tool_is_available(
            next(spec for spec in TOOL_SPECS if spec.name == name), document, config_path
        )
    ):
        raise ConfigError(f"tool is unavailable: {name}")
    agent = dict(document["agent"])
    tools = dict(agent.get("tools", {}))
    allowlist = list(tools.get("allowlist", []))
    if enabled and name not in allowlist:
        allowlist.append(name)
    if not enabled:
        allowlist = [item for item in allowlist if item != name]
    tools["allowlist"] = allowlist
    agent["tools"] = tools
    document["agent"] = agent
    _write_config(config_path, document)


PUBLIC_TOOLS = frozenset(PUBLIC_TO_INTERNAL) | {SERVER_LOCAL_TOOL}


def get_section(path: str | Path | None, scope: Literal["server", "agent"]) -> dict[str, Any]:
    config_path = _config_path(path)
    document = _load_yaml(config_path)
    _reject_legacy_secret_sections(document)
    if scope not in document:
        raise ConfigError(f"{scope} configuration is not initialized")
    section = document[scope]
    if not isinstance(section, Mapping):
        raise ConfigError(f"{scope} configuration must be a mapping")
    return copy.deepcopy(dict(section))


def _parse_value(value: str) -> Any:
    try:
        return yaml.safe_load(value)
    except yaml.YAMLError as exc:
        raise ConfigError("value is not valid YAML") from exc


def _set_nested(section: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    current = section
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def _delete_nested(section: dict[str, Any], key: str) -> None:
    parts = key.split(".")
    current: Any = section
    for part in parts[:-1]:
        if not isinstance(current, Mapping) or part not in current:
            return
        current = current[part]
    if isinstance(current, dict):
        current.pop(parts[-1], None)


def _canonical_key(scope: str, key: str) -> str:
    aliases = {
        ("agent", "agent_id"): "identity.id",
        ("agent", "server_url"): "relay_url",
        ("agent", "workspace_dir"): "workspace",
    }
    return aliases.get((scope, key), key)


def set_value(
    path: str | Path | None,
    scope: Literal["server", "agent"],
    key: str,
    value: str,
    *,
    catalog: CatalogSnapshot | None = None,
) -> None:
    config_path = _config_path(path)
    document = _load_yaml(config_path)
    _reject_legacy_secret_sections(document)
    if not isinstance(document.get(scope), Mapping):
        raise ConfigError(f"{scope} configuration is not initialized")
    section = copy.deepcopy(dict(document[scope]))
    canonical = _canonical_key(scope, key)
    if canonical not in _CONFIG_KEYS[scope]:
        raise ConfigError(f"unknown {scope} configuration key: {key}")
    parsed_value: Any = _parse_value(value)
    if canonical == "tools.allowlist" and isinstance(parsed_value, str):
        parsed_value = [item.strip() for item in parsed_value.split(",") if item.strip()]
    if canonical == "tools.allowlist" and isinstance(parsed_value, list):
        try:
            validate_agent_allowlist(parsed_value, catalog=catalog)
        except CatalogError as exc:
            raise ConfigError(str(exc)) from None
    _set_nested(section, canonical, parsed_value)
    document[scope] = section
    _write_config(config_path, document)


def set_secret(
    path: str | Path | None,
    scope: Literal["server", "agent"],
    name: str,
    value: str,
) -> None:
    if not value:
        raise ConfigError("secret cannot be empty")
    if scope == "agent" and name != "agent_token":
        raise ConfigError("Agent configuration only accepts agent_token")
    if scope == "server" and name not in {"mcp_token", "agent_token"}:
        raise ConfigError("unknown Server secret")
    config_path = _config_path(path)
    document = _load_yaml(config_path)
    _reject_legacy_secret_sections(document)
    if not isinstance(document.get(scope), Mapping):
        raise ConfigError(f"{scope} configuration is not initialized")
    if scope == "agent" and name != "agent_token":
        raise ConfigError("Agent configuration only accepts agent_token")
    key = "RELAY_MCP_TOKEN" if name == "mcp_token" else "RELAY_AGENT_TOKEN"
    _update_dotenv(dotenv_path(config_path), key, value)


def unset_value(path: str | Path | None, scope: Literal["server", "agent"], key: str) -> None:
    if scope == "agent" and key == "mcp_token":
        raise ConfigError("Agent configuration only accepts agent_token")
    config_path = _config_path(path)
    document = _load_yaml(config_path)
    _reject_legacy_secret_sections(document)
    if not isinstance(document.get(scope), Mapping):
        raise ConfigError(f"{scope} configuration is not initialized")
    if key in {"mcp_token", "agent_token"}:
        if scope == "agent" and key == "mcp_token":
            raise ConfigError("Agent configuration only accepts agent_token")
        if scope == "server" and key not in {"mcp_token", "agent_token"}:
            raise ConfigError("unknown Server secret")
        dotenv_key = "RELAY_MCP_TOKEN" if key == "mcp_token" else "RELAY_AGENT_TOKEN"
        _update_dotenv(dotenv_path(config_path), dotenv_key, None)
        return
    canonical = _canonical_key(scope, key)
    if canonical not in _CONFIG_KEYS[scope]:
        raise ConfigError(f"unknown {scope} configuration key: {key}")
    section = copy.deepcopy(dict(document[scope]))
    _delete_nested(section, canonical)
    defaults = _default_document(scope)
    document[scope] = _deep_merge(defaults, section)
    _write_config(config_path, document)


def load_agent_settings(
    path: str | Path | None,
    *,
    env: Mapping[str, str] | None = None,
    catalog: CatalogSnapshot | None = None,
    defer_tool_validation: bool = False,
) -> Any:
    config_path = _config_path(path)
    effective_env = os.environ if env is None else env
    _reject_token_file_environment(effective_env)
    try:
        document = _load_yaml(config_path)
    except ConfigError:
        if not (
            effective_env.get("RELAY_URL")
            and effective_env.get("RELAY_AGENT_WORKSPACE")
            and _token_source_present(path, "RELAY_AGENT_TOKEN", env=effective_env)
        ):
            raise
        document = {"agent": _default_document("agent")}
        document["agent"]["identity"]["id"] = effective_env.get("RELAY_AGENT_ID", str(uuid.uuid4()))
    report = _validate_agent(
        document,
        config_path,
        effective_env,
        require=True,
        catalog=catalog,
        defer_tool_validation=defer_tool_validation,
    )
    if not report.valid:
        raise ConfigError(_invalid_configuration_message("agent", report))
    section = _effective_agent(document, effective_env)
    token = _secret_value(document, "agent", "agent", config_path, effective_env)
    from .agent import AgentSettings

    return AgentSettings(**section.runtime_settings(token=token, config_path=config_path))


def load_server_runtime(path: str | Path | None, *, env: Mapping[str, str] | None = None) -> ServerRuntime:
    config_path = _config_path(path)
    effective_env = os.environ if env is None else env
    _reject_token_file_environment(effective_env)
    try:
        document = _load_yaml(config_path)
    except ConfigError:
        if not (
            _token_source_present(path, "RELAY_MCP_TOKEN", env=effective_env)
            and _token_source_present(path, "RELAY_AGENT_TOKEN", env=effective_env)
        ):
            raise
        document = {"server": _default_document("server")}
    report = _validate_server(document, config_path, effective_env, require=True)
    if not report.valid:
        raise ConfigError(_invalid_configuration_message("server", report))
    section = _effective_server(document, effective_env)
    mcp_token = _secret_value(document, "server", "mcp", config_path, effective_env)
    agent_token = _secret_value(document, "server", "agent", config_path, effective_env)
    from .server import RelaySettings

    settings = RelaySettings(
        **section.runtime_settings(mcp_token=mcp_token, agent_token=agent_token)
    )
    return ServerRuntime(settings=settings, host=section.host, port=section.port)


def cua_tool_summary(
    path: str | Path | None,
    *,
    env: Mapping[str, str] | None = None,
    catalog: CatalogSnapshot,
) -> CuaToolSummary:
    """Summarize CUA without expanding its full inventory in compact output."""
    config_path = _config_path(path)
    effective_env = os.environ if env is None else env
    try:
        document = _load_yaml(config_path)
    except ConfigError:
        if not (
            effective_env.get("RELAY_URL")
            and effective_env.get("RELAY_AGENT_WORKSPACE")
        ):
            raise
        document = {"agent": _default_document("agent")}
    _reject_legacy_secret_sections(document)
    agent = _effective_agent(document, effective_env)
    allowlist = list(agent.tools.allowlist)
    selected = catalog.select(allowlist)
    cua_entries = tuple(
        entry for entry in selected.entries if entry.provider_name == "cua"
    )
    selected_names = {
        name for name in allowlist if _is_dynamic_cua_public_name(name)
    }
    access = cua_access_for_allowlist(allowlist)
    if access == "custom" and selected_names:
        selected_cua = tuple(
            name for name in allowlist if _is_dynamic_cua_public_name(name)
        )
        for level in ("standard", "full"):
            try:
                expected = tuple(
                    name
                    for name in profile_public_names(level)
                    if catalog.entry(name).status != "blocked"
                )
            except CatalogError:
                continue
            if selected_cua == expected:
                access = level
                break
    return CuaToolSummary(
        access=access,
        enabled=sum(entry.status == "enabled" for entry in cua_entries),
        available=sum(entry.status in {"enabled", "disabled"} for entry in cua_entries),
        blocked=sum(entry.status == "blocked" for entry in cua_entries),
        new_names=tuple(
            entry.public_name
            for entry in cua_entries
            if entry.status == "disabled"
            and entry.public_name not in selected_names
            and entry.public_name not in ALL_PROFILE_PUBLIC_NAMES
        ),
    )


def _is_secret_output_key(key: str) -> bool:
    normalized = key.lower()
    return normalized != "secrets" and (
        is_sensitive_query_key(key)
        or normalized == "key"
        or (
            any(
                marker in normalized
                for marker in ("token", "secret", "password", "api_key", "api-key", "apikey")
            )
            and not normalized.endswith("_file")
        )
    )


def _redact_url_query(value: str) -> str:
    parsed = urlparse(value)
    if not parsed.query:
        return value
    query = "&".join(
        f"{name}=[REDACTED]" if separator and _is_secret_output_key(unquote_plus(name)) else part
        for part in parsed.query.split("&")
        for name, separator, _value in (part.partition("="),)
    )
    return parsed._replace(query=query).geturl()


def redact_for_output(value: Any, key: str = "") -> Any:
    """Return a recursively redacted copy suitable for safe presentation."""
    if _is_secret_output_key(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(child_key): redact_for_output(child_value, str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [redact_for_output(item, key) for item in value]
    if isinstance(value, str):
        return _redact_url_query(value)
    return value

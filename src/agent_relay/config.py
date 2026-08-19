"""Canonical YAML configuration, private dotenv credentials, and CLI helpers."""

from __future__ import annotations

import copy
import getpass
import os
import secrets
import stat
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping
from urllib.parse import urlparse

import yaml

from .catalog import (
    CatalogEntry,
    CatalogError,
    CatalogSnapshot,
)
from .catalog import discover_local_catalog as _discover_local_catalog
from .cua_profiles import (
    ALL_PROFILE_PUBLIC_NAMES,
    CuaAccessLevel,
    ReportedCuaAccess,
    cua_access_for_allowlist,
    is_cua_public_name,
    profile_public_names,
)
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


def _is_public_agent_tool(value: object) -> bool:
    return (
        isinstance(value, str)
        and (value in PUBLIC_TO_INTERNAL or _is_dynamic_cua_public_name(value))
    )


class ConfigError(ValueError):
    """A safe, user-facing configuration error with no secret interpolation."""


class CuaAccessCancelled(ConfigError):
    """The operator declined a requested Full CUA access change."""


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


_SERVER_DEFAULTS: dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8000,
    "mcp": {"allowed_hosts": [], "allowed_origins": []},
    "runtime": {
        "min_timeout_seconds": 0.1,
        "max_timeout_seconds": 30.0,
        "cancel_send_timeout_seconds": 0.25,
        "max_ws_message_bytes": 128 * 1024,
    },
}
_AGENT_DEFAULTS: dict[str, Any] = {
    "identity": {"id": ""},
    "relay_url": "ws://127.0.0.1:8000/ws/agent",
    "workspace": "./workspace",
    "tools": {"allowlist": []},
    "computer": {
        "allowed_app_name": None,
        "allowed_window_title": None,
        "startup_timeout_seconds": 15.0,
        "action_timeout_seconds": 10.0,
        "shutdown_timeout_seconds": 3.0,
        "max_elements": 300,
    },
    "runtime": {
        "heartbeat_interval_seconds": 15.0,
        "reconnect_min_seconds": 0.1,
        "reconnect_max_seconds": 5.0,
        "stable_session_seconds": 30.0,
        "max_ws_message_bytes": 128 * 1024,
        "command_timeout_seconds": 30.0,
        "stdout_limit": 24 * 1024,
        "stderr_limit": 24 * 1024,
    },
}

_CONFIG_KEYS: dict[str, frozenset[str]] = {
    "server": frozenset(
        {
            "host",
            "port",
            "mcp.allowed_hosts",
            "mcp.allowed_origins",
            "runtime.min_timeout_seconds",
            "runtime.max_timeout_seconds",
            "runtime.cancel_send_timeout_seconds",
            "runtime.max_ws_message_bytes",
        }
    ),
    "agent": frozenset(
        {
            "identity.id",
            "relay_url",
            "workspace",
            "tools.allowlist",
            "computer.allowed_app_name",
            "computer.allowed_window_title",
            "computer.startup_timeout_seconds",
            "computer.action_timeout_seconds",
            "computer.shutdown_timeout_seconds",
            "computer.max_elements",
            "runtime.heartbeat_interval_seconds",
            "runtime.reconnect_min_seconds",
            "runtime.reconnect_max_seconds",
            "runtime.stable_session_seconds",
            "runtime.max_ws_message_bytes",
            "runtime.command_timeout_seconds",
            "runtime.stdout_limit",
            "runtime.stderr_limit",
        }
    ),
}


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
    defaults = _SERVER_DEFAULTS if scope == "server" else _AGENT_DEFAULTS
    result = copy.deepcopy(defaults)
    if scope == "agent":
        result["identity"]["id"] = str(uuid.uuid4())
    return result


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


def _parse_int(value: object, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ConfigError("configuration integer is invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError("configuration integer is invalid") from exc
    if not minimum <= parsed <= maximum:
        raise ConfigError("configuration integer is out of range")
    return parsed


def _parse_float(value: object, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise ConfigError("configuration number is invalid")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError("configuration number is invalid") from exc
    if not minimum <= parsed <= maximum:
        raise ConfigError("configuration number is out of range")
    return parsed


def _env_value(env: Mapping[str, str], key: str) -> str | None:
    return env.get(key)


def _reject_token_file_environment(env: Mapping[str, str]) -> None:
    if "RELAY_MCP_TOKEN_FILE" in env or "RELAY_AGENT_TOKEN_FILE" in env:
        raise ConfigError("*_TOKEN_FILE is no longer supported; use .env")

def _effective_server(document: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    section = _deep_merge(_default_document("server"), document.get("server", {}))
    if (value := _env_value(env, "RELAY_SERVER_HOST")) is not None:
        section["host"] = value
    if (value := _env_value(env, "RELAY_SERVER_PORT")) is not None:
        section["port"] = value
    mcp = section.setdefault("mcp", {})
    if (value := _env_value(env, "RELAY_MCP_ALLOWED_HOSTS")) is not None:
        mcp["allowed_hosts"] = [item.strip() for item in value.split(",") if item.strip()]
    if (value := _env_value(env, "RELAY_MCP_ALLOWED_ORIGINS")) is not None:
        mcp["allowed_origins"] = [item.strip() for item in value.split(",") if item.strip()]
    runtime = section.setdefault("runtime", {})
    for env_key, field in (
        ("RELAY_MIN_TIMEOUT_SECONDS", "min_timeout_seconds"),
        ("RELAY_MAX_TIMEOUT_SECONDS", "max_timeout_seconds"),
        ("RELAY_CANCEL_SEND_TIMEOUT_SECONDS", "cancel_send_timeout_seconds"),
        ("RELAY_MAX_WS_MESSAGE_BYTES", "max_ws_message_bytes"),
    ):
        if (value := _env_value(env, env_key)) is not None:
            runtime[field] = value
    return section


def _effective_agent(document: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    section = _deep_merge(_default_document("agent"), document.get("agent", {}))
    if (value := _env_value(env, "RELAY_URL")) is not None:
        section["relay_url"] = value
    if (value := _env_value(env, "RELAY_AGENT_ID")) is not None:
        section.setdefault("identity", {})["id"] = value
    if (value := _env_value(env, "RELAY_AGENT_WORKSPACE")) is not None:
        section["workspace"] = value
    if (value := _env_value(env, "RELAY_AGENT_TOOLS")) is not None:
        section.setdefault("tools", {})["allowlist"] = [
            item.strip() for item in value.split(",") if item.strip()
        ]
    runtime = section.setdefault("runtime", {})
    for field in (
        "heartbeat_interval_seconds",
        "reconnect_min_seconds",
        "reconnect_max_seconds",
        "stable_session_seconds",
        "max_ws_message_bytes",
        "command_timeout_seconds",
        "stdout_limit",
        "stderr_limit",
    ):
        if (value := _env_value(env, "RELAY_AGENT_" + field.upper())) is not None:
            runtime[field] = value
    computer = section.setdefault("computer", {})
    for env_key, field in (
        ("RELAY_AGENT_COMPUTER_ALLOWED_APP_NAME", "allowed_app_name"),
        ("RELAY_AGENT_COMPUTER_ALLOWED_WINDOW_TITLE", "allowed_window_title"),
        ("RELAY_AGENT_COMPUTER_STARTUP_TIMEOUT_SECONDS", "startup_timeout_seconds"),
        ("RELAY_AGENT_COMPUTER_ACTION_TIMEOUT_SECONDS", "action_timeout_seconds"),
        ("RELAY_AGENT_COMPUTER_SHUTDOWN_TIMEOUT_SECONDS", "shutdown_timeout_seconds"),
        ("RELAY_AGENT_COMPUTER_MAX_ELEMENTS", "max_elements"),
    ):
        if (value := _env_value(env, env_key)) is not None:
            computer[field] = value
    return section


def discover_local_catalog(
    path: str | Path | None,
    *,
    env: Mapping[str, str] | None = None,
) -> CatalogSnapshot:
    """Discover local providers using the canonical Agent config overlay."""
    config_path = _config_path(path)
    effective_env = os.environ if env is None else env
    _reject_token_file_environment(effective_env)
    document = _load_yaml(config_path, required=False)
    _reject_legacy_secret_sections(document)
    agent = _effective_agent(document, effective_env)
    catalog_env = dict(effective_env)

    computer = agent.get("computer", {})
    if isinstance(computer, Mapping):
        computer_env_fields = (
            ("allowed_app_name", "RELAY_AGENT_COMPUTER_ALLOWED_APP_NAME"),
            ("allowed_window_title", "RELAY_AGENT_COMPUTER_ALLOWED_WINDOW_TITLE"),
            (
                "startup_timeout_seconds",
                "RELAY_AGENT_COMPUTER_STARTUP_TIMEOUT_SECONDS",
            ),
            (
                "action_timeout_seconds",
                "RELAY_AGENT_COMPUTER_ACTION_TIMEOUT_SECONDS",
            ),
            (
                "shutdown_timeout_seconds",
                "RELAY_AGENT_COMPUTER_SHUTDOWN_TIMEOUT_SECONDS",
            ),
        )
        for field, env_key in computer_env_fields:
            if computer.get(field) is not None:
                catalog_env.setdefault(env_key, str(computer[field]))

    tools = agent.get("tools", {})
    allowlist = tools.get("allowlist", []) if isinstance(tools, Mapping) else []
    if not isinstance(allowlist, list):
        raise ConfigError("tools.allowlist must be a list")
    return _discover_local_catalog(env=catalog_env, allowlist=allowlist)


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


def _identity_is_valid(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _url_is_valid(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = urlparse(value)
        parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {"ws", "wss"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def validate_relay_url(value: str) -> None:
    """Validate a Relay WebSocket URL without exposing user input in errors."""
    if not _url_is_valid(value):
        raise ConfigError("agent relay_url must be a ws:// or wss:// URL")


def validate_agent_transport(value: str) -> None:
    """Validate the structural Relay WebSocket URL contract."""
    validate_relay_url(value)


def _validate_root(document: Mapping[str, Any], report: list[ValidationIssue]) -> None:
    allowed = {"server", "agent"}
    unknown = sorted(set(document) - allowed)
    if unknown:
        report.append(ValidationIssue("ERROR", f"unknown root key(s): {', '.join(unknown)}"))


def _validate_known_keys(
    raw: Mapping[str, Any], scope: Literal["server", "agent"], issues: list[ValidationIssue]
) -> None:
    allowed = _CONFIG_KEYS[scope]

    def walk(value: Mapping[str, Any], prefix: str = "") -> None:
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if path not in allowed and not any(item.startswith(path + ".") for item in allowed):
                if path == "secrets" or path.startswith("secrets."):
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            "legacy secrets configuration is unsupported; create .env next to the YAML file",
                        )
                    )
                else:
                    issues.append(
                        ValidationIssue(
                            "ERROR", f"unknown {scope} configuration key: {path}"
                        )
                    )
                continue
            if isinstance(child, Mapping):
                walk(child, path)

    walk(raw)


def _validate_server(
    document: Mapping[str, Any], path: Path, env: Mapping[str, str], *, require: bool
) -> ValidationReport:
    issues: list[ValidationIssue] = []
    _validate_root(document, issues)
    raw = document.get("server")
    if raw is None:
        if require:
            issues.append(ValidationIssue("ERROR", "server section is missing"))
        else:
            issues.append(ValidationIssue("INFO", "server section is not configured"))
        return ValidationReport("server", tuple(issues))
    if not isinstance(raw, Mapping):
        return ValidationReport("server", (ValidationIssue("ERROR", "server section must be a mapping"),))
    _validate_known_keys(raw, "server", issues)
    section = _effective_server(document, env)
    host = section.get("host")
    if not isinstance(host, str) or not host.strip():
        issues.append(ValidationIssue("ERROR", "server host is invalid"))
    try:
        port = _parse_int(section.get("port"), minimum=1, maximum=65535)
        issues.append(ValidationIssue("INFO", f"port={port}"))
    except ConfigError:
        issues.append(ValidationIssue("ERROR", "server port is invalid"))
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
    try:
        _validate_runtime(section.get("runtime"), server=True)
    except ConfigError:
        issues.append(ValidationIssue("ERROR", "server runtime limits are invalid"))
    return ValidationReport("server", tuple(issues))


def _validate_runtime(raw: object, *, server: bool) -> None:
    if not isinstance(raw, Mapping):
        raise ConfigError("runtime must be a mapping")
    fields = (
        ("min_timeout_seconds", 0.0001, 3600.0),
        ("max_timeout_seconds", 0.0001, 3600.0),
        ("cancel_send_timeout_seconds", 0.0001, 5.0),
        ("max_ws_message_bytes", 1024, 1024 * 1024),
    ) if server else (
        ("heartbeat_interval_seconds", 0.0001, 3600.0),
        ("reconnect_min_seconds", 0.0001, 60.0),
        ("reconnect_max_seconds", 0.0001, 3600.0),
        ("stable_session_seconds", 1.0, 3600.0),
        ("max_ws_message_bytes", 1024, 1024 * 1024),
        ("command_timeout_seconds", 0.0001, 3600.0),
        ("stdout_limit", 0, 48 * 1024),
        ("stderr_limit", 0, 48 * 1024),
    )
    values: dict[str, float] = {}
    for key, minimum, maximum in fields:
        values[key] = _parse_float(raw.get(key), minimum=float(minimum), maximum=float(maximum))
    if not server and values["reconnect_min_seconds"] > values["reconnect_max_seconds"]:
        raise ConfigError("reconnect range is invalid")


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
        if require:
            issues.append(ValidationIssue("ERROR", "agent section is missing"))
        else:
            issues.append(ValidationIssue("INFO", "agent section is not configured"))
        return ValidationReport("agent", tuple(issues))
    if not isinstance(raw, Mapping):
        return ValidationReport("agent", (ValidationIssue("ERROR", "agent section must be a mapping"),))
    _validate_known_keys(raw, "agent", issues)
    section = _effective_agent(document, env)
    identity = section.get("identity")
    identity_value = identity.get("id") if isinstance(identity, Mapping) else None
    if not _identity_is_valid(identity_value):
        issues.append(ValidationIssue("ERROR", "agent identity.id must be a UUID"))
    relay_url = section.get("relay_url")
    if not _url_is_valid(relay_url):
        issues.append(ValidationIssue("ERROR", "agent relay_url must be a ws:// or wss:// URL"))
    else:
        parsed = urlparse(str(relay_url))
        if parsed.scheme == "ws":
            issues.append(
                ValidationIssue(
                    "INFO",
                    "transport=ws:// (unencrypted; intended for local or trusted LAN use)",
                )
            )
        else:
            issues.append(ValidationIssue("INFO", "transport=wss:// (TLS expected)"))
    workspace_value = section.get("workspace")
    try:
        workspace = _relative_path(workspace_value, path)
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
    tools = section.get("tools")
    allowlist = tools.get("allowlist") if isinstance(tools, Mapping) else None
    if not isinstance(allowlist, list) or any(not isinstance(item, str) for item in allowlist):
        issues.append(ValidationIssue("ERROR", "tools.allowlist must be a list of tool names"))
    else:
        if len(set(allowlist)) != len(allowlist):
            issues.append(ValidationIssue("ERROR", "tools.allowlist contains duplicates"))
        elif catalog is not None:
            try:
                catalog.validate_allowlist(allowlist)
            except CatalogError as exc:
                issues.append(ValidationIssue("ERROR", str(exc)))
        elif not defer_tool_validation:
            for name in allowlist:
                if name == SERVER_LOCAL_TOOL:
                    issues.append(ValidationIssue("ERROR", "relay_device_status is server-local and cannot be selected"))
                elif not _is_public_agent_tool(name):
                    issues.append(ValidationIssue("ERROR", f"unknown Agent tool: {name}"))
                elif name in PUBLIC_TO_INTERNAL and not _tool_is_available(
                    next(spec for spec in TOOL_SPECS if spec.name == name),
                    {"agent": section},
                    path,
                ):
                    issues.append(ValidationIssue("ERROR", f"Agent tool is unavailable: {name}"))
        if not allowlist:
            issues.append(ValidationIssue("INFO", "no tools enabled"))
    _validate_agent_computer(section.get("computer"), path, issues)
    try:
        _validate_runtime(section.get("runtime"), server=False)
    except ConfigError:
        issues.append(ValidationIssue("ERROR", "agent runtime limits are invalid"))
    return ValidationReport("agent", tuple(issues))


def _validate_agent_computer(raw: object, path: Path, issues: list[ValidationIssue]) -> None:
    del path
    if not isinstance(raw, Mapping):
        issues.append(ValidationIssue("ERROR", "computer section must be a mapping"))
        return
    values = [raw.get("allowed_app_name"), raw.get("allowed_window_title")]
    if any(value is not None for value in values):
        if not all(isinstance(value, str) and value for value in values):
            issues.append(ValidationIssue("ERROR", "computer configuration must provide all policy fields"))


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


def _render_report(report: ValidationReport) -> str:
    lines = [report.scope.capitalize()]
    for issue in report.issues:
        lines.append(f"[{issue.level}] {issue.message}")
    if report.valid:
        lines.append("result=valid")
    else:
        lines.append("result=invalid")
    return "\n".join(lines)


def render_validation(report: ValidationReport) -> str:
    return _render_report(report)


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
    if not isinstance(allowlist, list) or any(not isinstance(item, str) for item in allowlist):
        raise ConfigError("tools.allowlist must be a list of tool names")
    if len(set(allowlist)) != len(allowlist):
        raise ConfigError("tools.allowlist contains duplicates")
    return allowlist


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


def confirm_cua_access(
    level: str,
    catalog: CatalogSnapshot | None,
    *,
    assume_yes: bool = False,
) -> None:
    """Validate and, for Full, obtain the one required operator confirmation."""
    names = validate_cua_profile(level, catalog)
    if level != "full":
        if assume_yes:
            raise ConfigError("--yes is only valid with --cua-access full")
        return
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise ConfigError("full CUA access requires --yes when stdin is non-interactive")
    entries = {entry.public_name: entry for entry in (catalog.entries if catalog else ())}
    sensitive = sorted(
        {
            entry.risk
            for name in names
            if (entry := entries.get(name)) is not None
            and entry.risk in {"destructive", "admin"}
        }
    )
    print("Full CUA access enables all non-blocked desktop and browser tools.")
    if sensitive:
        print(f"Sensitive categories: {', '.join(sensitive)}.")
    try:
        answer = input("Enable Full CUA access? [y/N] ")
    except (EOFError, OSError) as exc:
        raise ConfigError("could not read the Full CUA confirmation") from exc
    if answer.strip().lower() not in {"y", "yes"}:
        raise CuaAccessCancelled("CUA access update cancelled")


def apply_cua_access(
    allowlist: list[str] | tuple[str, ...],
    level: str,
    catalog: CatalogSnapshot | None,
    *,
    assume_yes: bool = False,
) -> list[str]:
    """Return a stable allowlist after atomically replacing only its CUA part."""
    names = validate_cua_profile(level, catalog)
    if level == "full":
        confirm_cua_access(level, catalog, assume_yes=assume_yes)
    elif assume_yes:
        raise ConfigError("--yes is only valid with --cua-access full")
    non_cua = [name for name in allowlist if not _is_dynamic_cua_public_name(name)]
    result = non_cua + list(names)
    if catalog is not None:
        try:
            catalog.validate_allowlist(result)
        except CatalogError as exc:
            raise ConfigError(str(exc)) from None
    else:
        invalid = [
            name
            for name in result
            if (
                name == SERVER_LOCAL_TOOL
                or not _is_public_agent_tool(name)
                or (
                    name in PUBLIC_TO_INTERNAL
                    and not _tool_is_available(
                        next(spec for spec in TOOL_SPECS if spec.name == name),
                        {},
                        Path("."),
                    )
                )
            )
        ]
        if invalid:
            raise ConfigError(f"unavailable or unknown Agent tool: {invalid[0]}")
    return result


def update_cua_access(
    path: str | Path | None,
    level: str,
    *,
    catalog: CatalogSnapshot | None = None,
    assume_yes: bool = False,
) -> bool:
    """Apply a CUA profile in one YAML write; return False only on no-op."""
    config_path = _config_path(path)
    document = _load_yaml(config_path)
    _reject_legacy_secret_sections(document)
    section = document.get("agent")
    if not isinstance(section, Mapping):
        raise ConfigError("agent configuration is not initialized")
    current = _existing_allowlist(section)
    updated = apply_cua_access(current, level, catalog, assume_yes=assume_yes)
    if updated == current:
        return True
    agent_section = copy.deepcopy(dict(section))
    tools = dict(agent_section.get("tools", {}))
    tools["allowlist"] = updated
    agent_section["tools"] = tools
    document["agent"] = agent_section
    _write_config(config_path, document)
    return True


def _validate_existing_static_allowlist(
    section: Mapping[str, Any],
    path: Path,
) -> None:
    allowlist = _existing_allowlist(section)
    invalid = [
        name
        for name in allowlist
        if (
            name == SERVER_LOCAL_TOOL
            or not _is_public_agent_tool(name)
            or (
                name in PUBLIC_TO_INTERNAL
                and not _tool_is_available(
                next(spec for spec in TOOL_SPECS if spec.name == name),
                section,
                path,
                )
            )
        )
    ]
    if invalid:
        raise ConfigError(f"unavailable or unknown Agent tool: {invalid[0]}")


def init_config(
    path: str | Path | None,
    scope: Literal["server", "agent"],
    *,
    force: bool = False,
    token: str | None = None,
    tools: list[str] | None = None,
    use_stdin: bool = False,
    env: Mapping[str, str] | None = None,
    catalog: CatalogSnapshot | None = None,
    relay_url: str | None = None,
    workspace: str | Path | None = None,
    cua_access: str | None = None,
    assume_yes: bool = False,
) -> Path:
    config_path = _config_path(path)
    effective_env = os.environ if env is None else env
    _reject_token_file_environment(effective_env)
    if scope == "server" and (cua_access is not None or assume_yes):
        raise ConfigError("CUA access options are only valid for Agent configuration")
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
                assume_yes=assume_yes,
            )
        if catalog is not None:
            try:
                catalog.validate_allowlist(requested_tools)
            except CatalogError as exc:
                raise ConfigError(str(exc)) from None
        else:
            invalid = [
                name
                for name in requested_tools
                if (
                    name == SERVER_LOCAL_TOOL
                    or not _is_public_agent_tool(name)
                    or (
                        name in PUBLIC_TO_INTERNAL
                        and not _tool_is_available(
                            next(spec for spec in TOOL_SPECS if spec.name == name),
                            section,
                            config_path,
                        )
                    )
                )
            ]
            if invalid:
                raise ConfigError(f"unavailable or unknown Agent tool: {invalid[0]}")
        section.setdefault("tools", {})["allowlist"] = requested_tools
        workspace = _relative_path(section["workspace"], config_path)
        _ensure_private_directory(workspace)
        section["workspace"] = _relative_config_value(section["workspace"], config_path)
        document["agent"] = section
        _write_config(config_path, document)
        if token is None and use_stdin:
            token = sys.stdin.readline().strip()
        if (
            token is None
            and existing is not None
            and "RELAY_AGENT_TOKEN" not in effective_env
        ):
            token = dotenv_values.get("RELAY_AGENT_TOKEN")
        if token is None:
            token = getpass.getpass("Agent token: ").strip()
        if not token:
            raise ConfigError("agent token cannot be empty")
        if "RELAY_AGENT_TOKEN" not in effective_env:
            _update_dotenv(dotenv_file, "RELAY_AGENT_TOKEN", token)
    return config_path


def _relative_config_value(value: object, path: Path) -> str:
    if isinstance(value, str) and not Path(value).expanduser().is_absolute():
        return value
    return str(_relative_path(value, path))


def select_tools_interactively(
    document: Mapping[str, Any],
    path: Path,
    *,
    catalog: CatalogSnapshot | None = None,
) -> list[str]:
    if not document:
        try:
            document = _load_yaml(path, required=False)
        except ConfigError:
            document = {}
    print("Select Agent tools (comma-separated numbers; empty enables none):")
    if catalog is not None:
        entries = [
            entry
            for entry in catalog.entries
            if not _is_dynamic_cua_public_name(entry.public_name)
        ]
        for index, entry in enumerate(entries, start=1):
            print(
                f"  {index}. {entry.public_name} [{entry.risk}] "
                f"{entry.status} - {entry.description}"
            )
        if not entries or not sys.stdin.isatty():
            return []
        answer = input("Tools: ").strip()
        if not answer:
            return []
        try:
            selected = {int(item.strip()) for item in answer.split(",") if item.strip()}
        except ValueError as exc:
            raise ConfigError("tool selection must be a comma-separated list of numbers") from exc
        if any(index < 1 or index > len(entries) for index in selected):
            raise ConfigError("tool selection contains an invalid number")
        selected_names = [entries[index - 1].public_name for index in sorted(selected)]
        try:
            catalog.validate_allowlist(selected_names)
        except CatalogError as exc:
            raise ConfigError(str(exc)) from None
        return selected_names

    specs = [
        spec
        for spec in TOOL_SPECS
        if spec.source != "server" and _tool_is_available(spec, document, path)
    ]
    for index, spec in enumerate(specs, start=1):
        print(f"  {index}. {spec.name} - {spec.description}")
    if not specs or not sys.stdin.isatty():
        return []
    answer = input("Tools: ").strip()
    if not answer:
        return []
    try:
        selected = {int(item.strip()) for item in answer.split(",") if item.strip()}
    except ValueError as exc:
        raise ConfigError("tool selection must be a comma-separated list of numbers") from exc
    if any(index < 1 or index > len(specs) for index in selected):
        raise ConfigError("tool selection contains an invalid number")
    return [specs[index - 1].name for index in sorted(selected)]


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
    tools = agent.get("tools", {})
    allowlist = list(tools.get("allowlist", []) if isinstance(tools, Mapping) else [])
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
    effective_document["agent"] = agent
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
    if catalog is not None:
        try:
            catalog.entry(name)
        except CatalogError as exc:
            raise ConfigError(str(exc)) from None
        if enabled:
            try:
                catalog.validate_allowlist([name])
            except CatalogError as exc:
                raise ConfigError(str(exc)) from None
    elif not _is_public_agent_tool(name):
        raise ConfigError(f"unknown Agent tool: {name}")
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
        if catalog is not None:
            try:
                catalog.validate_allowlist(parsed_value)
            except CatalogError as exc:
                raise ConfigError(str(exc)) from None
        else:
            invalid = [
                item
                for item in parsed_value
                if (
                    item == SERVER_LOCAL_TOOL
                    or not _is_public_agent_tool(item)
                    or (
                        item in PUBLIC_TO_INTERNAL
                        and not _tool_is_available(
                        next(spec for spec in TOOL_SPECS if spec.name == item),
                        section,
                        config_path,
                        )
                    )
                )
            ]
            if invalid:
                raise ConfigError(f"unknown or server-local Agent tool: {invalid[0]}")
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
    identity = section["identity"]["id"]
    computer = section["computer"]
    runtime = section["runtime"]
    token = _secret_value(document, "agent", "agent", config_path, effective_env)
    workspace = _relative_path(section["workspace"], config_path)
    from .agent import AgentSettings

    return AgentSettings(
        server_url=section["relay_url"],
        device_id=identity,
        agent_id=identity,
        agent_token=token,
        workspace=workspace,
        tools_allowlist=tuple(section["tools"]["allowlist"]),
        heartbeat_interval_seconds=float(runtime["heartbeat_interval_seconds"]),
        reconnect_min_seconds=float(runtime["reconnect_min_seconds"]),
        reconnect_max_seconds=float(runtime["reconnect_max_seconds"]),
        stable_session_seconds=float(runtime["stable_session_seconds"]),
        max_ws_message_bytes=int(runtime["max_ws_message_bytes"]),
        command_timeout_seconds=float(runtime["command_timeout_seconds"]),
        stdout_limit=int(runtime["stdout_limit"]),
        stderr_limit=int(runtime["stderr_limit"]),
        computer_allowed_app_name=computer["allowed_app_name"],
        computer_allowed_window_title=computer["allowed_window_title"],
        computer_startup_timeout_seconds=float(computer["startup_timeout_seconds"]),
        computer_action_timeout_seconds=float(computer["action_timeout_seconds"]),
        computer_shutdown_timeout_seconds=float(computer["shutdown_timeout_seconds"]),
        computer_max_elements=int(computer["max_elements"]),
    )


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
    runtime = section["runtime"]
    mcp = section["mcp"]
    from .server import RelaySettings

    settings = RelaySettings(
        agent_token=agent_token,
        mcp_token=mcp_token,
        bind_host=str(section["host"]),
        mcp_allowed_hosts=tuple(mcp["allowed_hosts"]),
        mcp_allowed_origins=tuple(mcp["allowed_origins"]),
        min_timeout_seconds=float(runtime["min_timeout_seconds"]),
        max_timeout_seconds=float(runtime["max_timeout_seconds"]),
        cancel_send_timeout_seconds=float(runtime["cancel_send_timeout_seconds"]),
        max_ws_message_bytes=int(runtime["max_ws_message_bytes"]),
    )
    return ServerRuntime(
        settings=settings,
        host=str(section["host"]),
        port=_parse_int(section["port"], minimum=1, maximum=65535),
    )


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
    tools = agent.get("tools", {})
    allowlist = list(tools.get("allowlist", []) if isinstance(tools, Mapping) else [])
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


def render_tools(
    path: str | Path | None,
    *,
    env: Mapping[str, str] | None = None,
    catalog: CatalogSnapshot | None = None,
    show_all: bool = False,
) -> str:
    statuses = tool_statuses(path, env=env, catalog=catalog)
    rows = ["Tool\tSource\tStatus\tRisk\tDescription"]
    if catalog is not None and not show_all:
        statuses = [
            (spec, status)
            for spec, status in statuses
            if not _is_dynamic_cua_public_name(spec.name)
        ]
    for spec, status in statuses:
        rows.append(
            f"{spec.name}\t{spec.source}\t{status}\t{spec.risk}\t{spec.description}"
        )
    if catalog is not None and not show_all:
        summary = cua_tool_summary(path, env=env, catalog=catalog)
        rows.append(
            "CUA\tcua\t"
            f"{summary.access}\tinteraction\t"
            f"{summary.enabled} enabled, {summary.available} available, "
            f"{summary.blocked} blocked, {len(summary.new_names)} new tools not selected"
        )
        if summary.new_names:
            rows.append(
                "CUA new\tcua\tdisabled\tinteraction\t"
                + ", ".join(summary.new_names)
            )
    return "\n".join(rows)


def render_section(path: str | Path | None, scope: Literal["server", "agent"]) -> str:
    return yaml.safe_dump(
        _redact_for_output(get_section(path, scope)),
        sort_keys=False,
        allow_unicode=True,
    )


def _redact_for_output(value: Any, key: str = "") -> Any:
    normalized = key.lower()
    is_secret_key = (
        normalized != "secrets"
        and (
            normalized in {"token", "mcp_token", "agent_token", "api_key", "password", "secret"}
            or any(marker in normalized for marker in ("token", "secret", "password", "api_key"))
            and not normalized.endswith("_file")
        )
    )
    if is_secret_key:
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(child_key): _redact_for_output(child_value, str(child_key)) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [_redact_for_output(item, key) for item in value]
    return value


def doctor(
    path: str | Path | None,
    *,
    env: Mapping[str, str] | None = None,
    catalog: CatalogSnapshot | None = None,
) -> tuple[str, bool]:
    reports = [
        validate_document(path, "server", env=env, require=False),
        validate_document(path, "agent", env=env, require=False, catalog=catalog),
    ]
    lines = ["Agent Relay doctor"]
    for report in reports:
        lines.append("")
        lines.append(report.scope.capitalize())
        lines.extend(f"[{issue.level}] {issue.message}" for issue in report.issues)
    lines.append("")
    lines.append(
        f"Summary: {sum(len(report.errors) for report in reports)} error(s), "
        f"{sum(len(report.warnings) for report in reports)} warning(s)"
    )
    return "\n".join(lines), all(report.valid for report in reports)

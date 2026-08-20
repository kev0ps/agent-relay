"""Local provider discovery and explicit Agent tool consent policy."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal

from pydantic import ValidationError

from .capabilities.system import SYSTEM_PROVIDER_DESCRIPTORS
from .capabilities.terminal import TERMINAL_PROVIDER_DESCRIPTORS
from .cua_profiles import is_cua_public_name
from .diagnostics import debug as _debug_log
from .json_bounds import JsonValue
from .output_models import ProviderToolResult
from .provider_tools import ProviderRiskClass, ProviderToolDescriptor
from .providers.base import ProviderToolClient, ProviderToolError, bounded_descriptors
from .providers.in_process import InProcessProviderToolClient


def _debug_cua_discovery_failure(provider_name: str, category: str) -> None:
    if provider_name == "cua":
        _debug_log(f"cua provider discovery failed: category={category}")


DEFAULT_RESERVED_PUBLIC_NAMES = frozenset(
    {
        "relay_system_ping",
        "relay_terminal_exec",
        "relay_device_status",
    }
)
DEFAULT_AGENT_PUBLIC_NAMES = DEFAULT_RESERVED_PUBLIC_NAMES - {"relay_device_status"}
CatalogStatus = Literal["disabled", "enabled", "unavailable", "blocked"]
ProviderStatus = Literal["available", "unavailable"]


class CatalogError(ValueError):
    """A safe, local catalog or explicit-selection error."""


class _EphemeralCuaCatalogClient:
    """Start one owned CUA process only long enough to read its inventory."""

    def __init__(self, capability: object) -> None:
        self._capability = capability

    async def list_tools(self) -> Sequence[ProviderToolDescriptor]:
        start = getattr(self._capability, "start")
        list_tools = getattr(self._capability, "list_tools")
        close = getattr(self._capability, "aclose")
        try:
            await start()
            return await list_tools()
        finally:
            await close()

    async def call_tool(
        self, tool_name: str, arguments: Mapping[str, JsonValue]
    ) -> ProviderToolResult:
        del tool_name, arguments
        raise ProviderToolError("catalog discovery client does not dispatch")

    async def close(self) -> None:
        close = getattr(self._capability, "aclose")
        await close()


@dataclass(frozen=True, slots=True)
class ProviderRegistration:
    """A locally configured provider and optional reference inventory.

    ``known_tools`` is reference metadata used only to display an unavailable
    provider. It is never a fallback inventory when a provider client is
    available: the provider's current ``tools/list`` response is authoritative.
    """

    name: str
    client: ProviderToolClient | None
    known_tools: tuple[ProviderToolDescriptor, ...] = ()
    allow_reserved_public_names: bool = False

    def __post_init__(self) -> None:
        if not _is_provider_name(self.name):
            raise CatalogError("invalid provider name")
        if len(self.known_tools) > 128:
            raise CatalogError("provider reference inventory is too large")


@dataclass(frozen=True, slots=True)
class ProviderState:
    name: str
    status: ProviderStatus
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CatalogPolicy:
    """Default deny rules applied after provider descriptors are bounded."""

    blocked_tool_names: frozenset[str] = frozenset(
        {
            "page",
            "execute_javascript",
            "javascript",
            "arbitrary_code",
            "arbitrary_execute",
        }
    )
    blocked_name_fragments: frozenset[str] = frozenset(
        {
            "execute_javascript",
            "arbitrary_code",
            "module_path",
            "executable_path",
            "javascript",
            "script",
        }
    )
    cua_interaction_names: frozenset[str] = frozenset(
        {
            "launch_app",
            "bring_to_front",
            "set_window_frame",
            "invoke_menu",
            "click",
            "double_click",
            "right_click",
            "drag",
            "type_text",
            "press_key",
            "hotkey",
            "scroll",
            "move_cursor",
            "zoom",
            "browser_prepare",
            "browser_navigate",
            "browser_click",
            "browser_type",
        }
    )
    cua_read_only_names: frozenset[str] = frozenset(
        {
            "list_apps",
            "list_windows",
            "get_window_state",
            "get_accessibility_tree",
            "get_config",
            "get_recording_state",
            "get_agent_cursor_state",
            "get_browser_state",
            "health_report",
            "check_for_update",
            "verify_state",
        }
    )
    cua_destructive_names: frozenset[str] = frozenset(
        {
            "kill_app",
            "set_value",
            "start_recording",
            "stop_recording",
            "replay_trajectory",
            "clipboard_write",
            "browser_set_input_files",
            "browser_download",
            "browser_dialog",
        }
    )
    cua_admin_names: frozenset[str] = frozenset(
        {
            "set_config",
            "start_session",
            "end_session",
            "set_agent_cursor_enabled",
            "set_agent_cursor_motion",
            "check_permissions",
            "install_ffmpeg",
            "set_agent_cursor_theme",
            "escalate_session",
            "get_session_state",
        }
    )
    cua_blocked_names: frozenset[str] = frozenset(
        {
            "page",
            "get_desktop_state",
            "get_screen_size",
            "get_cursor_position",
            "get_accessibility_tree",
            "debug_window_info",
            "clipboard_read",
            "browser_pointer",
            "screenshot",
        }
    )

    def classify(self, descriptor: ProviderToolDescriptor) -> ProviderRiskClass:
        name = descriptor.tool_name.lower()
        if descriptor.risk == "blocked":
            return "blocked"
        if name in self.blocked_tool_names or any(
            fragment in name for fragment in self.blocked_name_fragments
        ):
            return "blocked"
        if descriptor.provider_name == "cua":
            if name in self.cua_blocked_names:
                return "blocked"
            if name in self.cua_interaction_names:
                return "interaction"
            if name in self.cua_read_only_names:
                return "read_only"
            if name in self.cua_destructive_names:
                return "destructive"
            if name in self.cua_admin_names:
                return "admin"
            # Dynamic provider inventories are untrusted metadata. A new CUA
            # tool must be classified explicitly before it can cross the
            # Agent allowlist boundary; visibility is retained for diagnostics.
            return "blocked"
        return descriptor.risk


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One provider candidate with its effective public identity and status."""

    descriptor: ProviderToolDescriptor
    public_name: str
    provider_name: str
    tool_name: str
    source: str
    description: str
    risk: ProviderRiskClass
    status: CatalogStatus
    reason: str | None = None

    @property
    def internal_name(self) -> str:
        return self.tool_name


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    """An immutable discovery result that can be re-applied to an allowlist."""

    entries: tuple[CatalogEntry, ...]
    providers: tuple[ProviderState, ...]

    def entry(self, public_name: str) -> CatalogEntry:
        for entry in self.entries:
            if entry.public_name == public_name:
                return entry
        raise CatalogError(f"unknown Agent tool: {public_name}")

    @property
    def selected_descriptors(self) -> tuple[ProviderToolDescriptor, ...]:
        return tuple(
            entry.descriptor for entry in self.entries if entry.status == "enabled"
        )

    @property
    def selected_public_names(self) -> tuple[str, ...]:
        return tuple(
            entry.public_name for entry in self.entries if entry.status == "enabled"
        )

    @property
    def unavailable_providers(self) -> tuple[str, ...]:
        return tuple(
            provider.name
            for provider in self.providers
            if provider.status == "unavailable"
        )

    def select(self, allowlist: Sequence[str]) -> CatalogSnapshot:
        """Apply explicit public names without enabling any implicit candidate."""
        names = _validate_names(allowlist)
        known = {entry.public_name for entry in self.entries}
        unknown = [name for name in names if name not in known]
        if unknown:
            raise CatalogError(f"unknown Agent tool: {unknown[0]}")
        selected = set(names)
        return replace(
            self,
            entries=tuple(
                replace(
                    entry,
                    status=(
                        "enabled"
                        if entry.public_name in selected and entry.status
                        not in {"blocked", "unavailable"}
                        else entry.status
                        if entry.status in {"blocked", "unavailable"}
                        else "disabled"
                    ),
                )
                for entry in self.entries
            ),
        )

    def validate_allowlist(self, allowlist: Sequence[str]) -> None:
        """Reject unknown, duplicate, blocked, or unavailable selections."""
        names = _validate_names(allowlist)
        known = {entry.public_name for entry in self.entries}
        for name in names:
            if name not in known:
                raise CatalogError(f"unknown Agent tool: {name}")
            entry = self.entry(name)
            if entry.status == "blocked":
                raise CatalogError(f"blocked Agent tool: {name}")
            if entry.status == "unavailable":
                raise CatalogError(f"unavailable Agent tool: {name}")


def validate_agent_allowlist(
    allowlist: Sequence[str],
    *,
    catalog: CatalogSnapshot | None = None,
    defer_unknown: bool = False,
) -> tuple[str, ...]:
    """Validate config-facing Agent tool consent in one catalog boundary.

    Without a discovered catalog, only built-in tools and syntactically valid
    dynamic CUA names are known. Runtime catalog discovery can be deferred for
    other provider names, while shape and duplicate checks remain mandatory.
    """
    names = _validate_names(allowlist)
    if catalog is not None:
        catalog.validate_allowlist(names)
        return names
    if defer_unknown:
        return names
    for name in names:
        if name == "relay_device_status":
            raise CatalogError(
                "relay_device_status is server-local and cannot be selected"
            )
        if name not in DEFAULT_AGENT_PUBLIC_NAMES and not is_cua_public_name(name):
            raise CatalogError(f"unknown Agent tool: {name}")
    return names


class CatalogService:
    """Discover provider inventories and apply the local consent policy."""

    def __init__(
        self,
        providers: Sequence[ProviderRegistration],
        *,
        policy: CatalogPolicy | None = None,
        reserved_public_names: Sequence[str] = tuple(DEFAULT_RESERVED_PUBLIC_NAMES),
    ) -> None:
        names = [provider.name for provider in providers]
        if len(set(names)) != len(names):
            raise CatalogError("duplicate provider name")
        self._providers = tuple(providers)
        self._policy = policy or CatalogPolicy()
        self._reserved_public_names = frozenset(reserved_public_names)

    async def discover(
        self, allowlist: Sequence[str] = ()
    ) -> CatalogSnapshot:
        entries: list[CatalogEntry] = []
        provider_states: list[ProviderState] = []
        public_names: set[str] = set()

        for provider in self._providers:
            descriptors: tuple[ProviderToolDescriptor, ...]
            available = True
            reason: str | None = None
            if provider.client is None:
                available = False
                reason = "provider is not configured"
                descriptors = _bounded_reference_tools(provider.known_tools)
            else:
                try:
                    raw_tools = await provider.client.list_tools()
                    descriptors = bounded_descriptors(
                        raw_tools, aggregate=provider.name != "cua"
                    )
                except ProviderToolError as exc:
                    if "invalid provider tool inventory" in str(exc):
                        _debug_cua_discovery_failure(provider.name, "invalid-inventory")
                        raise CatalogError("invalid provider inventory") from None
                    _debug_cua_discovery_failure(provider.name, "provider-tool")
                    available = False
                    reason = "provider is unavailable"
                    descriptors = _bounded_reference_tools(provider.known_tools)
                except (ValidationError, TypeError, ValueError) as exc:
                    _debug_cua_discovery_failure(provider.name, "invalid-inventory")
                    raise CatalogError("invalid provider inventory") from exc
                except Exception:
                    _debug_cua_discovery_failure(provider.name, "other")
                    available = False
                    reason = "provider is unavailable"
                    descriptors = _bounded_reference_tools(provider.known_tools)

            provider_states.append(
                ProviderState(
                    name=provider.name,
                    status="available" if available else "unavailable",
                    reason=reason,
                )
            )
            for descriptor in descriptors:
                entry = _entry_for(
                    provider.name,
                    descriptor,
                    policy=self._policy,
                    available=available,
                    reason=reason,
                )
                if (
                    entry.public_name in self._reserved_public_names
                    and not provider.allow_reserved_public_names
                ):
                    raise CatalogError(
                        f"reserved public tool name collision: {entry.public_name}"
                    )
                if entry.public_name in public_names:
                    raise CatalogError(
                        f"public tool name collision: {entry.public_name}"
                    )
                public_names.add(entry.public_name)
                entries.append(entry)

        snapshot = CatalogSnapshot(tuple(entries), tuple(provider_states))
        return snapshot.select(allowlist)


async def _catalog_probe_handler(
    arguments: Mapping[str, JsonValue],
) -> ProviderToolResult:
    raise ProviderToolError("catalog discovery client does not dispatch")


def _local_reference_tools() -> dict[str, tuple[ProviderToolDescriptor, ...]]:
    return {
        "system": SYSTEM_PROVIDER_DESCRIPTORS,
        "terminal": TERMINAL_PROVIDER_DESCRIPTORS,
    }


def _in_process_catalog_client(
    descriptors: Sequence[ProviderToolDescriptor],
) -> InProcessProviderToolClient:
    bounded = bounded_descriptors(descriptors)
    return InProcessProviderToolClient(
        bounded,
        {descriptor.tool_name: _catalog_probe_handler for descriptor in bounded},
    )


def _configured_cua_catalog_client(
    env: Mapping[str, str],
) -> ProviderToolClient | None:
    app_name = env.get("RELAY_AGENT_COMPUTER_ALLOWED_APP_NAME")
    window_title = env.get("RELAY_AGENT_COMPUTER_ALLOWED_WINDOW_TITLE")

    def number(name: str, default: float) -> float:
        try:
            return float(env.get(name, default))
        except (TypeError, ValueError):
            return default

    try:
        from .capabilities.computer import ComputerCapability

        capability = ComputerCapability(
            app_name,
            window_title,
            startup_timeout_seconds=number(
                "RELAY_AGENT_COMPUTER_STARTUP_TIMEOUT_SECONDS", 15.0
            ),
            action_timeout_seconds=number(
                "RELAY_AGENT_COMPUTER_ACTION_TIMEOUT_SECONDS", 10.0
            ),
            shutdown_timeout_seconds=number(
                "RELAY_AGENT_COMPUTER_SHUTDOWN_TIMEOUT_SECONDS", 3.0
            ),
            environ=dict(env),
        )
    except (OSError, TypeError, ValueError) as error:
        category = (
            "os-error"
            if isinstance(error, OSError)
            else "type-error"
            if isinstance(error, TypeError)
            else "value-error"
        )
        if env.get("RELAY_NATIVE_DEBUG") == "1":
            _debug_log(
                "cua catalog construction failed: "
                f"stage=capability-init category={category}",
                enabled=True,
            )
        return None
    return _EphemeralCuaCatalogClient(capability)


def local_provider_registrations(
    env: Mapping[str, str] | None = None,
    providers: Mapping[str, ProviderToolClient] | None = None,
    *,
    allowlist: Sequence[str] | None = None,
) -> tuple[ProviderRegistration, ...]:
    """Build the production local provider registry for catalog discovery.

    System and Terminal are exposed through real in-process provider clients,
    so ``CatalogService`` obtains their current bounded inventory through
    ``tools/list``. CUA is always initialized through its installed Python
    package and is started only for an ephemeral ``tools/list`` discovery
    session when no runtime provider is supplied.
    """
    effective_env = {} if env is None else dict(env)
    tools = _local_reference_tools()
    runtime_providers = {} if providers is None else dict(providers)
    unknown = set(runtime_providers) - {"system", "terminal", "cua"}
    if unknown:
        raise CatalogError(f"unknown local provider: {sorted(unknown)[0]}")
    registrations: list[ProviderRegistration] = []
    for provider_name in ("system", "terminal", "cua"):
        descriptors = tools.get(provider_name, ())
        client = runtime_providers.get(provider_name)
        if client is None and provider_name in {"system", "terminal"}:
            client = _in_process_catalog_client(descriptors)
        if client is None and provider_name == "cua":
            client = _configured_cua_catalog_client(effective_env)
        registrations.append(
            ProviderRegistration(
                provider_name,
                client,
                known_tools=descriptors,
                allow_reserved_public_names=True,
            )
        )
    return tuple(registrations)


def discover_local_catalog(
    *,
    env: Mapping[str, str] | None = None,
    allowlist: Sequence[str] | None = None,
    providers: Mapping[str, ProviderToolClient] | None = None,
) -> CatalogSnapshot:
    """Synchronously discover the local provider catalog for CLI boundaries."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("discover_local_catalog cannot run inside an event loop")
    service = CatalogService(
        local_provider_registrations(env, providers, allowlist=allowlist)
    )
    return asyncio.run(service.discover(() if allowlist is None else allowlist))


def public_tool_name(provider_name: str, tool_name: str) -> str:
    """Map provider identities to a stable, collision-checked relay namespace."""
    provider = _slug(provider_name)
    tool = _slug(tool_name)
    if not provider or not tool:
        raise CatalogError("provider and tool names must contain alphanumeric characters")
    public_name = f"relay_{provider}_{tool}"
    if len(public_name) > 128:
        raise CatalogError("public Agent tool name is too long")
    return public_name


def _entry_for(
    provider_name: str,
    descriptor: ProviderToolDescriptor,
    *,
    policy: CatalogPolicy,
    available: bool,
    reason: str | None,
) -> CatalogEntry:
    public_name = public_tool_name(provider_name, descriptor.tool_name)
    risk = policy.classify(descriptor)
    normalized = descriptor.model_dump(mode="python", exclude_none=True)
    normalized.update(
        {
            "provider_name": provider_name,
            "public_name": public_name,
            "risk": risk,
        }
    )
    try:
        bounded = ProviderToolDescriptor.model_validate(normalized)
    except (ValidationError, TypeError, ValueError) as exc:
        raise CatalogError("invalid provider inventory") from exc
    status: CatalogStatus
    if not available:
        status = "unavailable"
    elif risk == "blocked":
        status = "blocked"
    else:
        status = "disabled"
    return CatalogEntry(
        descriptor=bounded,
        public_name=public_name,
        provider_name=provider_name,
        tool_name=bounded.tool_name,
        source=provider_name,
        description=bounded.description,
        risk=risk,
        status=status,
        reason=reason if not available else "blocked by local policy" if risk == "blocked" else None,
    )


def _bounded_reference_tools(
    values: Sequence[ProviderToolDescriptor],
) -> tuple[ProviderToolDescriptor, ...]:
    try:
        return bounded_descriptors(values)
    except (ProviderToolError, TypeError, ValueError) as exc:
        raise CatalogError("invalid provider inventory") from exc


def _validate_names(values: Sequence[str]) -> tuple[str, ...]:
    if any(not isinstance(value, str) or not value for value in values):
        raise CatalogError("Agent tool names must be non-empty strings")
    names = tuple(values)
    if len(set(names)) != len(names):
        raise CatalogError("Agent tool allowlist contains duplicates")
    return names


def _is_provider_name(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value))


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()


# Short names make the catalog boundary convenient for adapters and tests.
ProviderToolCandidate = CatalogEntry
ToolCandidate = CatalogEntry
ProviderCatalog = CatalogSnapshot


__all__ = [
    "DEFAULT_AGENT_PUBLIC_NAMES",
    "DEFAULT_RESERVED_PUBLIC_NAMES",
    "CatalogEntry",
    "CatalogError",
    "CatalogPolicy",
    "CatalogService",
    "CatalogSnapshot",
    "ProviderCatalog",
    "ProviderRegistration",
    "ProviderState",
    "ProviderToolCandidate",
    "ToolCandidate",
    "public_tool_name",
    "validate_agent_allowlist",
]

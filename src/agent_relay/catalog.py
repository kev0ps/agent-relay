"""Local provider discovery and explicit Agent tool consent policy."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal

from pydantic import ValidationError

from .json_bounds import JsonValue
from .output_models import ProviderToolResult
from .provider_tools import ProviderRiskClass, ProviderToolDescriptor
from .providers.base import ProviderToolClient, ProviderToolError, bounded_descriptors
from .providers.in_process import InProcessProviderToolClient

DEFAULT_RESERVED_PUBLIC_NAMES = frozenset(
    {
        "relay_system_ping",
        "relay_terminal_exec",
        "relay_browser_list_tabs",
        "relay_browser_navigate",
        "relay_browser_snapshot",
        "relay_browser_fill",
        "relay_browser_click",
        "relay_browser_scroll",
        "relay_browser_type",
        "relay_browser_back",
        "relay_computer_capture",
        "relay_computer_click",
        "relay_computer_type",
        "relay_device_status",
    }
)
CatalogStatus = Literal["disabled", "enabled", "unavailable", "blocked"]
ProviderStatus = Literal["available", "unavailable"]


class CatalogError(ValueError):
    """A safe, local catalog or explicit-selection error."""


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
        {"execute_javascript", "arbitrary_code", "module_path", "executable_path"}
    )

    def classify(self, descriptor: ProviderToolDescriptor) -> ProviderRiskClass:
        name = descriptor.tool_name.lower()
        if descriptor.risk == "blocked":
            return "blocked"
        if name in self.blocked_tool_names or any(
            fragment in name for fragment in self.blocked_name_fragments
        ):
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
                    descriptors = bounded_descriptors(raw_tools)
                except ProviderToolError as exc:
                    if "invalid provider tool inventory" in str(exc):
                        raise CatalogError("invalid provider inventory") from None
                    available = False
                    reason = "provider is unavailable"
                    descriptors = _bounded_reference_tools(provider.known_tools)
                except (ValidationError, TypeError, ValueError) as exc:
                    raise CatalogError("invalid provider inventory") from exc
                except Exception:
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


def _reference_descriptor(
    provider_name: str,
    tool_name: str,
    description: str,
    risk: ProviderRiskClass,
) -> ProviderToolDescriptor:
    return ProviderToolDescriptor(
        provider_name=provider_name,
        tool_name=tool_name,
        public_name=tool_name,
        description=description,
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        risk=risk,
    )


def _local_reference_tools() -> dict[str, tuple[ProviderToolDescriptor, ...]]:
    browser_reference: tuple[tuple[str, str, ProviderRiskClass], ...] = (
        ("list_tabs", "list browser tabs", "read_only"),
        ("navigate", "navigate a browser tab", "interaction"),
        ("snapshot", "read semantic browser content", "read_only"),
        ("fill", "fill a semantic browser element", "interaction"),
        ("click", "click a semantic browser element", "interaction"),
        ("scroll", "scroll a browser page", "interaction"),
        ("type", "type into a semantic browser element", "interaction"),
        ("back", "navigate browser history backward", "interaction"),
    )
    computer_reference: tuple[tuple[str, str, ProviderRiskClass], ...] = (
        ("capture", "capture bounded desktop semantics", "read_only"),
        ("click", "click a captured desktop element", "interaction"),
        ("type", "type into a captured desktop element", "interaction"),
    )
    return {
        "system": (
            _reference_descriptor(
                "system", "ping", "fixed local health check", "read_only"
            ),
        ),
        "terminal": (
            _reference_descriptor(
                "terminal", "exec", "fixed allowlisted terminal command", "interaction"
            ),
        ),
        "browser": tuple(
            _reference_descriptor("browser", name, description, risk)
            for name, description, risk in browser_reference
        ),
        "computer": tuple(
            _reference_descriptor("computer", name, description, risk)
            for name, description, risk in computer_reference
        ),
    }


def _in_process_catalog_client(
    descriptors: Sequence[ProviderToolDescriptor],
) -> InProcessProviderToolClient:
    bounded = bounded_descriptors(descriptors)
    return InProcessProviderToolClient(
        bounded,
        {descriptor.tool_name: _catalog_probe_handler for descriptor in bounded},
    )


def local_provider_registrations(
    env: Mapping[str, str] | None = None,
    providers: Mapping[str, ProviderToolClient] | None = None,
) -> tuple[ProviderRegistration, ...]:
    """Build the production local provider registry for catalog discovery.

    System and Terminal are exposed through real in-process provider clients,
    so ``CatalogService`` obtains their current bounded inventory through
    ``tools/list``. Browser and Computer Use reference names stay unavailable
    until a real runtime provider client is supplied through ``providers``;
    configuration alone is never treated as provider availability.
    """
    del env  # reserved for provider factories that consume environment settings
    tools = _local_reference_tools()
    runtime_providers = {} if providers is None else dict(providers)
    unknown = set(runtime_providers) - set(tools)
    if unknown:
        raise CatalogError(f"unknown local provider: {sorted(unknown)[0]}")
    registrations: list[ProviderRegistration] = []
    for provider_name in ("system", "terminal", "browser", "computer"):
        descriptors = tools[provider_name]
        client = runtime_providers.get(provider_name)
        if client is None and provider_name in {"system", "terminal"}:
            client = _in_process_catalog_client(descriptors)
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
    allowlist: Sequence[str] = (),
    providers: Mapping[str, ProviderToolClient] | None = None,
) -> CatalogSnapshot:
    """Synchronously discover the local provider catalog for CLI boundaries."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("discover_local_catalog cannot run inside an event loop")
    service = CatalogService(local_provider_registrations(env, providers))
    return asyncio.run(service.discover(allowlist))


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
]

"""Constrained in-process Browser provider using a persistent Playwright profile."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Protocol, cast
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..json_bounds import MAX_JSON_COLLECTION_ITEMS, JsonObject, JsonValue
from ..output_models import ProviderToolResult
from ..protocol import (
    MAX_BROWSER_ELEMENT_VALUE_LENGTH,
    MAX_BROWSER_ELEMENTS,
    MAX_BROWSER_FILL_VALUE_LENGTH,
    MAX_BROWSER_NAME_LENGTH,
    MAX_BROWSER_PAGE_TEXT_LENGTH,
    MAX_BROWSER_ROLE_LENGTH,
    MAX_BROWSER_TITLE_LENGTH,
    MAX_BROWSER_TYPE_TEXT_LENGTH,
    MAX_BROWSER_URL_LENGTH,
)
from ..provider_tools import ProviderToolDescriptor
from ..providers.base import UnknownProviderToolError


class LocalActionError(RuntimeError):
    """A browser operation failed without exposing backend details."""


class BrowserUnavailableError(LocalActionError):
    """The owned browser context is no longer safe to use."""


class BrowserStartupError(BrowserUnavailableError):
    """The configured persistent browser could not be launched safely."""


BrowserOriginPolicy = Literal["allowlist", "any"]
_BROWSER_LOCATOR_STRATEGIES = ("role", "label", "placeholder", "text", "test_id")


class _BrowserEmptyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _BrowserNavigateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    url: str = Field(min_length=1, max_length=MAX_BROWSER_URL_LENGTH)


class _BrowserLocator(BaseModel):
    """A closed, provider-owned locator that is resolved afresh per action."""

    model_config = ConfigDict(extra="forbid", strict=True)

    by: Literal["role", "label", "placeholder", "text", "test_id"]
    role: str | None = Field(default=None, min_length=1, max_length=MAX_BROWSER_ROLE_LENGTH)
    name: str | None = Field(default=None, min_length=1, max_length=MAX_BROWSER_NAME_LENGTH)
    value: str | None = Field(default=None, min_length=1, max_length=MAX_BROWSER_NAME_LENGTH)
    exact: bool = True
    index: int | None = Field(default=None, ge=0, lt=MAX_BROWSER_ELEMENTS)

    @model_validator(mode="after")
    def _strategy_fields(self) -> "_BrowserLocator":
        if self.by == "role":
            if self.role is None or self.value is not None:
                raise ValueError("role locator requires role and forbids value")
        elif self.value is None or self.role is not None or self.name is not None:
            raise ValueError("locator strategy requires one value")
        return self


class _BrowserFillArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    locator: _BrowserLocator
    value: str = Field(min_length=1, max_length=MAX_BROWSER_FILL_VALUE_LENGTH)


class _BrowserClickArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    locator: _BrowserLocator


class _BrowserScrollArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    direction: Literal["up", "down"]


class _BrowserTypeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    locator: _BrowserLocator
    text: str = Field(min_length=1, max_length=MAX_BROWSER_TYPE_TEXT_LENGTH)


@dataclass(frozen=True)
class BrowserSnapshot:
    url: str
    title: str
    text: str
    elements: tuple[dict[str, object], ...]


class BrowserSession(Protocol):
    url: str
    title: str

    async def snapshot(self) -> BrowserSnapshot: ...
    async def locate(self, locator: Mapping[str, JsonValue]) -> Any: ...
    async def navigate(self, url: str) -> None: ...
    async def fill(self, locator: Any, value: str) -> None: ...
    async def type(self, locator: Any, text: str) -> None: ...
    async def click(self, locator: Any) -> None: ...
    async def state(self, locator: Any) -> tuple[bool, bool, bool, bool, str]: ...
    async def scroll(self, direction: str) -> None: ...
    async def back(self) -> bool: ...
    async def reset(self) -> None: ...
    async def wait_unavailable(self) -> None: ...
    async def aclose(self) -> None: ...
    def ensure(self, *, allow_blank: bool = False) -> None: ...


def _empty_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


def _locator_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "by": {"type": "string", "enum": list(_BROWSER_LOCATOR_STRATEGIES)},
            "role": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_BROWSER_ROLE_LENGTH,
            },
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_BROWSER_NAME_LENGTH,
            },
            "value": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_BROWSER_NAME_LENGTH,
            },
            "exact": {"type": "boolean"},
            "index": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_BROWSER_ELEMENTS - 1,
            },
        },
        "required": ["by"],
        "additionalProperties": False,
    }


def _descriptor(
    tool_name: str,
    description: str,
    input_schema: dict[str, object],
    risk: Literal["read_only", "interaction"],
) -> ProviderToolDescriptor:
    return ProviderToolDescriptor(
        provider_name="browser",
        tool_name=tool_name,
        public_name=tool_name,
        description=description,
        input_schema=cast(JsonObject, input_schema),
        risk=risk,
    )


def _locator_argument_schema(field: str) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "locator": _locator_schema(),
            field: {
                "type": "string",
                "minLength": 1,
                "maxLength": (
                    min(MAX_BROWSER_FILL_VALUE_LENGTH, MAX_JSON_COLLECTION_ITEMS)
                    if field == "value"
                    else min(MAX_BROWSER_TYPE_TEXT_LENGTH, MAX_JSON_COLLECTION_ITEMS)
                ),
            },
        },
        "required": ["locator", field],
        "additionalProperties": False,
    }


_BROWSER_DESCRIPTORS = (
    _descriptor("list_tabs", "list the owned browser pages", _empty_schema(), "read_only"),
    _descriptor(
        "navigate",
        "navigate the owned browser page",
        {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": min(MAX_BROWSER_URL_LENGTH, MAX_JSON_COLLECTION_ITEMS),
                }
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "interaction",
    ),
    _descriptor("snapshot", "read bounded provider-native browser content", _empty_schema(), "read_only"),
    _descriptor(
        "fill",
        "fill a freshly resolved browser locator",
        _locator_argument_schema("value"),
        "interaction",
    ),
    _descriptor(
        "click",
        "click a freshly resolved browser locator",
        {
            "type": "object",
            "properties": {"locator": _locator_schema()},
            "required": ["locator"],
            "additionalProperties": False,
        },
        "interaction",
    ),
    _descriptor(
        "scroll",
        "scroll the owned browser page",
        {
            "type": "object",
            "properties": {"direction": {"type": "string", "enum": ["up", "down"]}},
            "required": ["direction"],
            "additionalProperties": False,
        },
        "interaction",
    ),
    _descriptor(
        "type",
        "type into a freshly resolved browser locator",
        _locator_argument_schema("text"),
        "interaction",
    ),
    _descriptor("back", "navigate the owned browser history backward", _empty_schema(), "interaction"),
)
_BROWSER_DESCRIPTOR_BY_NAME = {descriptor.tool_name: descriptor for descriptor in _BROWSER_DESCRIPTORS}
BROWSER_PROVIDER_DESCRIPTORS = _BROWSER_DESCRIPTORS


def _browser_arguments(model: type[BaseModel], arguments: Mapping[str, JsonValue]) -> BaseModel:
    try:
        return model.model_validate(arguments)
    except ValidationError:
        raise LocalActionError() from None


async def _await_shared_cleanup(task: asyncio.Task[None]) -> None:
    """Delay caller cancellation until an internally owned cleanup is terminal."""
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
            if task.done():
                break
    if cancellation is not None:
        raise cancellation


def normalize_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid origin") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("invalid origin")
    if "*" in parsed.hostname:
        raise ValueError("invalid origin")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("invalid origin")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("invalid origin")
    default = 80 if parsed.scheme.lower() == "http" else 443
    host = parsed.hostname.lower()
    hostpart = f"[{host}]" if ":" in host else host
    return f"{parsed.scheme.lower()}://{hostpart}" + (f":{port}" if port and port != default else "")


def origin_of_url(value: str) -> str:
    parsed = urlsplit(value)
    return normalize_origin(urlunsplit((parsed.scheme, parsed.netloc, "", "", "")))


class BrowserCapability:
    """Provider-neutral Browser capability with no opaque Relay element handles."""

    tools = frozenset(f"browser.{descriptor.tool_name}" for descriptor in _BROWSER_DESCRIPTORS)

    def __init__(
        self,
        user_data_dir: Path,
        allowed_origins: tuple[str, ...],
        *,
        headless: bool = False,
        startup_timeout_seconds: float = 15,
        action_timeout_seconds: float = 10,
        origin_policy: BrowserOriginPolicy = "allowlist",
        adapter_factory: Callable[..., BrowserSession | Awaitable[BrowserSession]] | None = None,
    ) -> None:
        if not isinstance(user_data_dir, Path) or not user_data_dir.is_absolute():
            raise ValueError("browser user data directory must be absolute")
        if origin_policy not in {"allowlist", "any"}:
            raise ValueError("invalid browser origin policy")
        self._user_data_dir = user_data_dir
        self._origins = frozenset(normalize_origin(item) for item in allowed_origins)
        if origin_policy == "allowlist" and not self._origins:
            raise ValueError("allowed origins required")
        if origin_policy == "any" and self._origins:
            raise ValueError("allowed origins cannot be combined with any origin policy")
        self._origin_policy = origin_policy
        self._headless = headless
        self._startup_timeout = startup_timeout_seconds
        self._action_timeout = action_timeout_seconds
        self._factory = adapter_factory or _RealSession.create
        self._session_lock = asyncio.Lock()
        self._session: BrowserSession | None = None
        self._teardown_tasks: dict[int, asyncio.Task[None]] = {}
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False

    async def list_tools(self) -> tuple[ProviderToolDescriptor, ...]:
        return _BROWSER_DESCRIPTORS

    async def start(self) -> None:
        async with self._session_lock:
            if self._closed:
                raise BrowserUnavailableError()
            if self._session is not None:
                return
            await self._drain_teardowns()
            if self._closed:
                raise BrowserUnavailableError()
            try:
                made = self._factory(
                    self._user_data_dir,
                    self._origins,
                    self._headless,
                    self._startup_timeout,
                    self._action_timeout,
                    self._origin_policy,
                )
                self._session = (
                    await asyncio.wait_for(made, self._startup_timeout)
                    if inspect.isawaitable(made)
                    else made
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                raise BrowserStartupError() from None


    def _ready(self, *, allow_blank: bool = False) -> BrowserSession:
        if self._session is None:
            raise BrowserUnavailableError()
        session = self._session
        ensure = getattr(session, "ensure", None)
        if ensure is not None:
            ensure(allow_blank=allow_blank)
        return session

    async def call_tool(
        self, tool_name: str, arguments: Mapping[str, JsonValue]
    ) -> ProviderToolResult:
        short_name = tool_name.removeprefix("browser.")
        if short_name not in _BROWSER_DESCRIPTOR_BY_NAME:
            raise UnknownProviderToolError("unknown provider tool")
        result = await self._call_tool(short_name, arguments)
        if isinstance(result, ProviderToolResult):
            return result
        return ProviderToolResult.model_validate({"content": [], "structuredContent": result})

    async def invoke(self, message: Any) -> dict[str, object]:
        """Compatibility seam for CapabilityProviderClient's v2 local envelope."""
        tool_name = str(message.tool_name).removeprefix("browser.")
        result = await self._call_tool(tool_name, message.arguments)
        if isinstance(result, ProviderToolResult):
            return result.model_dump(mode="json", by_alias=True, exclude_none=True)
        return result

    async def _call_tool(
        self, tool_name: str, arguments: Mapping[str, JsonValue]
    ) -> dict[str, object] | ProviderToolResult:
        owned_session = self._session
        try:
            if tool_name == "list_tabs":
                _browser_arguments(_BrowserEmptyArguments, arguments)
                session = self._ready(allow_blank=True)
                return {"tabs": [self._tab(session, allow_blank=True)]}
            if tool_name == "snapshot":
                _browser_arguments(_BrowserEmptyArguments, arguments)
                return await self._snapshot()
            if tool_name == "navigate":
                parsed = _browser_arguments(_BrowserNavigateArguments, arguments)
                assert isinstance(parsed, _BrowserNavigateArguments)
                return await self._navigate(parsed.url)
            if tool_name == "fill":
                parsed = _browser_arguments(_BrowserFillArguments, arguments)
                assert isinstance(parsed, _BrowserFillArguments)
                return await self._fill(parsed.locator.model_dump(mode="json"), parsed.value)
            if tool_name == "click":
                parsed = _browser_arguments(_BrowserClickArguments, arguments)
                assert isinstance(parsed, _BrowserClickArguments)
                return await self._click(parsed.locator.model_dump(mode="json"))
            if tool_name == "scroll":
                parsed = _browser_arguments(_BrowserScrollArguments, arguments)
                assert isinstance(parsed, _BrowserScrollArguments)
                return await self._scroll(parsed.direction)
            if tool_name == "type":
                parsed = _browser_arguments(_BrowserTypeArguments, arguments)
                assert isinstance(parsed, _BrowserTypeArguments)
                return await self._type(parsed.locator.model_dump(mode="json"), parsed.text)
            if tool_name == "back":
                _browser_arguments(_BrowserEmptyArguments, arguments)
                return await self._back()
            raise UnknownProviderToolError("unknown provider tool")
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(self._cancel_invocation(owned_session))
            await _await_shared_cleanup(cleanup)
            raise
        except (LocalActionError, UnknownProviderToolError):
            raise
        except Exception:
            raise LocalActionError() from None

    def _tab(self, session: BrowserSession, *, allow_blank: bool = False) -> dict[str, object]:
        if not (allow_blank and session.url == "about:blank"):
            self._check_origin(session.url)
        return {
            "title": session.title[:MAX_BROWSER_TITLE_LENGTH],
            "url": session.url[:MAX_BROWSER_URL_LENGTH],
        }

    def _check_origin(self, url: str) -> None:
        try:
            origin = origin_of_url(url)
        except ValueError:
            raise LocalActionError() from None
        if self._origin_policy == "any" or origin in self._origins:
            return
        raise LocalActionError()

    async def _snapshot(self) -> dict[str, object]:
        session = self._ready()
        self._check_origin(session.url)
        snap = await asyncio.wait_for(session.snapshot(), self._action_timeout)
        self._check_origin(snap.url)
        elements: list[dict[str, object]] = []
        for raw in snap.elements[:MAX_BROWSER_ELEMENTS]:
            if "element_id" in raw or "handle" in raw:
                raise LocalActionError()
            locator = raw.get("locator")
            if not isinstance(locator, Mapping):
                raise LocalActionError()
            try:
                checked = _BrowserLocator.model_validate(locator)
            except ValidationError:
                raise LocalActionError() from None
            item = dict(raw)
            if item.get("input_type") in {"password", "file"}:
                item["value"] = None
            item.pop("input_type", None)
            item["locator"] = checked.model_dump(mode="json", exclude_none=True)
            elements.append(item)
        return {
            "title": snap.title[:MAX_BROWSER_TITLE_LENGTH],
            "url": snap.url[:MAX_BROWSER_URL_LENGTH],
            "text": snap.text[:MAX_BROWSER_PAGE_TEXT_LENGTH],
            "elements": elements,
        }

    async def _navigate(self, url: str) -> dict[str, object]:
        self._check_origin(url)
        session = self._ready(allow_blank=True)
        await asyncio.wait_for(session.navigate(url), self._action_timeout)
        self._ready()
        self._check_origin(session.url)
        return self._action()

    async def _resolve(self, locator: Mapping[str, JsonValue]) -> tuple[BrowserSession, object]:
        session = self._ready()
        handle = await asyncio.wait_for(session.locate(locator), self._action_timeout)
        visible, enabled, _, _, _ = await asyncio.wait_for(
            session.state(handle), self._action_timeout
        )
        if not (visible and enabled):
            raise LocalActionError()
        return session, handle

    async def _fill(self, locator: Mapping[str, JsonValue], value: str) -> dict[str, object]:
        session, handle = await self._resolve(locator)
        _, _, editable, _, input_type = await session.state(handle)
        if not editable or input_type in {"password", "file"}:
            raise LocalActionError()
        await asyncio.wait_for(session.fill(handle, value), self._action_timeout)
        self._ready()
        self._check_origin(session.url)
        return self._action()

    async def _type(self, locator: Mapping[str, JsonValue], text: str) -> dict[str, object]:
        session, handle = await self._resolve(locator)
        _, _, editable, _, input_type = await session.state(handle)
        if not editable or input_type in {"password", "file"}:
            raise LocalActionError()
        await asyncio.wait_for(session.type(handle, text), self._action_timeout)
        self._ready()
        self._check_origin(session.url)
        return self._action()

    async def _click(self, locator: Mapping[str, JsonValue]) -> dict[str, object]:
        session, handle = await self._resolve(locator)
        _, _, _, clickable, _ = await session.state(handle)
        if not clickable:
            raise LocalActionError()
        await asyncio.wait_for(session.click(handle), self._action_timeout)
        self._ready()
        self._check_origin(session.url)
        return self._action()

    async def _scroll(self, direction: str) -> dict[str, object]:
        session = self._ready()
        await asyncio.wait_for(session.scroll(direction), self._action_timeout)
        self._ready()
        self._check_origin(session.url)
        return self._action()

    async def _back(self) -> dict[str, object]:
        session = self._ready()
        moved = await asyncio.wait_for(session.back(), self._action_timeout)
        if not moved:
            raise LocalActionError()
        self._ready()
        self._check_origin(session.url)
        return self._action()

    def _action(self) -> dict[str, object]:
        session = self._ready()
        return {
            "success": True,
            "title": session.title[:MAX_BROWSER_TITLE_LENGTH],
            "url": session.url[:MAX_BROWSER_URL_LENGTH],
        }

    async def _cancel_invocation(self, session: BrowserSession | None) -> None:
        if session is None:
            return
        try:
            await asyncio.wait_for(session.reset(), self._action_timeout)
        except Exception:
            pass

    async def wait_unavailable(self) -> None:
        session = self._session
        if session is None:
            raise BrowserUnavailableError()
        await session.wait_unavailable()
        async with self._session_lock:
            if self._session is session:
                self._session = None
                self._schedule_teardown(session)

    def _schedule_teardown(self, session: BrowserSession) -> None:
        key = id(session)
        if key in self._teardown_tasks:
            return
        task = asyncio.create_task(self._close_session(session, key))
        self._teardown_tasks[key] = task

        def completed(done: asyncio.Task[None]) -> None:
            if self._teardown_tasks.get(key) is done:
                self._teardown_tasks.pop(key, None)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(completed)

    async def _drain_teardowns(self) -> None:
        while self._teardown_tasks:
            active = tuple(self._teardown_tasks.values())
            await asyncio.gather(*(asyncio.shield(task) for task in active))

    async def _close_session(self, session: BrowserSession, _key: int) -> None:
        try:
            await asyncio.wait_for(session.aclose(), max(self._startup_timeout, self._action_timeout * 6))
        except Exception:
            pass

    async def aclose(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._aclose_owned())
        await _await_shared_cleanup(self._close_task)

    async def close(self) -> None:
        await self.aclose()

    async def _aclose_owned(self) -> None:
        async with self._session_lock:
            self._closed = True
            session, self._session = self._session, None
        if session:
            self._schedule_teardown(session)
        await self._drain_teardowns()


class _RealSession:
    """Lazy Playwright adapter; importing the base package never imports Playwright."""

    @classmethod
    async def create(
        cls,
        user_data_dir: Path,
        origins: frozenset[str],
        headless: bool,
        startup: float,
        action: float,
        origin_policy: BrowserOriginPolicy = "allowlist",
    ) -> "_RealSession":
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserUnavailableError() from exc
        pw = await async_playwright().start()
        context = None
        try:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=headless,
                accept_downloads=False,
                service_workers="block",
            )
            obj = cls(pw, context, origins, action, origin_policy)
            await obj._arm()
            pages = getattr(context, "pages", [])
            obj.page = pages[0] if pages else await context.new_page()
            obj._bind_page(obj.page)
            obj.url = obj.page.url
            obj.title = ""
            return obj
        except BaseException:
            if context is not None:
                try:
                    await context.close()
                except BaseException:
                    pass
            try:
                await pw.stop()
            except BaseException:
                pass
            raise

    def __init__(
        self,
        pw: object,
        context: object,
        origins: frozenset[str],
        action: float,
        origin_policy: BrowserOriginPolicy = "allowlist",
    ) -> None:
        self.pw, self.context = pw, context
        self.origins, self.action, self.origin_policy = origins, action, origin_policy
        self.page = None
        self.url, self.title = "about:blank", ""
        self.unavailable = asyncio.Event()
        self.tasks: set[asyncio.Task[object]] = set()
        self.suppress_close = False
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False

    def _origin_allowed(self, url: str) -> bool:
        try:
            origin = origin_of_url(url)
        except ValueError:
            return False
        return self.origin_policy == "any" or origin in self.origins

    async def _arm(self) -> None:
        async def route_handler(route: object) -> None:
            try:
                if self._origin_allowed(route.request.url):
                    await route.continue_()
                else:
                    self.unavailable.set()
                    await route.abort()
            except Exception:
                self.unavailable.set()
                try:
                    await route.abort()
                except Exception:
                    pass

        await self.context.route("**/*", route_handler)
        if hasattr(self.context, "route_web_socket"):

            async def close_websocket(ws: object) -> None:
                try:
                    await ws.close()
                except Exception:
                    pass

            def reject_websocket(ws: object) -> None:
                self.unavailable.set()
                self._spawn(close_websocket(ws))

            await self.context.route_web_socket("**", reject_websocket)
        self._arm_browser_events()

    def _arm_browser_events(self) -> None:
        if hasattr(self.context, "on"):
            self.context.on("close", lambda _context: self.unavailable.set())
        browser = getattr(self.context, "browser", None)
        if browser is not None and hasattr(browser, "on"):
            browser.on("disconnected", lambda _browser: self.unavailable.set())

    def _spawn(self, awaitable: Awaitable[object]) -> None:
        task = asyncio.create_task(awaitable)
        self.tasks.add(task)

        def completed(done: asyncio.Task[object]) -> None:
            self.tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(completed)

    def _bind_page(self, page: object) -> None:
        async def reject_dialog(dialog: object) -> None:
            try:
                await dialog.dismiss()
            except Exception:
                pass

        async def reject_download(download: object) -> None:
            try:
                await download.cancel()
            except Exception:
                pass

        async def reject_popup(popup: object) -> None:
            try:
                await popup.close()
            except Exception:
                pass

        def check_frame(frame: object) -> None:
            try:
                if frame.parent_frame is not None or not self._origin_allowed(frame.url):
                    self.unavailable.set()
            except ValueError:
                if frame.url != "about:blank":
                    self.unavailable.set()

        page.on("dialog", lambda dialog: (self.unavailable.set(), self._spawn(reject_dialog(dialog))))
        page.on("download", lambda download: (self.unavailable.set(), self._spawn(reject_download(download))))
        page.on("popup", lambda popup: (self.unavailable.set(), self._spawn(reject_popup(popup))))
        page.on("filechooser", lambda _: self.unavailable.set())
        page.on("websocket", lambda _: self.unavailable.set())
        page.on("crash", lambda _page: self.unavailable.set())
        page.on("close", lambda _page: None if self.suppress_close else self.unavailable.set())
        page.on("framenavigated", check_frame)

    async def _refresh(self) -> None:
        self.url = self.page.url
        self.title = await self.page.locator("html").evaluate(
            "(_node, limit) => document.title.slice(0, limit)",
            MAX_BROWSER_TITLE_LENGTH,
        )
        self._ensure()

    def _ensure(self) -> None:
        context_is_closed = False
        is_closed = getattr(self.context, "is_closed", None)
        if callable(is_closed):
            context_is_closed = bool(is_closed())
        if (
            self._closed
            or self.unavailable.is_set()
            or context_is_closed
            or self.page is None
            or self.page.is_closed()
            or len(self.page.frames) != 1
        ):
            self.unavailable.set()
            raise BrowserUnavailableError()
        self_url = self.page.url
        if self_url != "about:blank" and not self._origin_allowed(self_url):
            self.unavailable.set()
            raise BrowserUnavailableError()

    def ensure(self, *, allow_blank: bool = False) -> None:
        self._ensure()
        if not allow_blank and self.page.url == "about:blank":
            raise LocalActionError()

    async def snapshot(self) -> BrowserSnapshot:
        await self._refresh()
        page = self.page
        if page is None:
            raise BrowserUnavailableError()
        text = await page.locator("body").evaluate(
            "(node, limit) => (node.innerText || '').slice(0, limit)",
            MAX_BROWSER_PAGE_TEXT_LENGTH,
        )
        result: list[dict[str, object]] = []
        for role, editable, clickable in (
            ("textbox", True, False),
            ("button", False, True),
            ("link", False, True),
            ("checkbox", False, True),
            ("radio", False, True),
            ("combobox", True, False),
        ):
            locator = page.get_by_role(role)
            try:
                count = await locator.count()
            except Exception:
                if role != "button":
                    continue
                count = 0
            if role == "button" and count == 0:
                locator = page.locator(
                    'button, input[type="button"], input[type="submit"], [role="button"]'
                )
                try:
                    count = await locator.count()
                except Exception:
                    continue
            for index in range(min(count, MAX_BROWSER_ELEMENTS - len(result))):
                item = locator.nth(index)
                try:
                    if not await item.is_visible():
                        continue
                    try:
                        aria = await item.aria_snapshot(timeout=self.action * 1000, depth=0)
                        name = _parse_root_aria_name(aria, role)
                    except Exception:
                        if role != "button":
                            raise
                        name = await item.evaluate(
                            """(node, limit) => {
                                const labelled = node.getAttribute('aria-label');
                                const source = labelled === null
                                    ? (node.textContent || '')
                                    : labelled;
                                return source.trim().slice(0, limit);
                            }""",
                            MAX_BROWSER_NAME_LENGTH,
                        )
                        if type(name) is not str or not name:
                            raise LocalActionError()
                    name = name[:MAX_BROWSER_NAME_LENGTH]
                    input_type = (await item.get_attribute("type") or "").lower()
                    value = None
                    if editable and input_type not in {"password", "file"}:
                        value = await item.evaluate(
                            "(node, limit) => ('value' in node) ? String(node.value).slice(0, limit) : null",
                            MAX_BROWSER_ELEMENT_VALUE_LENGTH,
                        )
                    result.append(
                        {
                            "locator": {
                                "by": "role",
                                "role": role,
                                "name": name or None,
                                "exact": True,
                                "index": index,
                            },
                            "role": role[:MAX_BROWSER_ROLE_LENGTH] or "unknown",
                            "name": name,
                            "value": None if value is None else value[:MAX_BROWSER_ELEMENT_VALUE_LENGTH],
                            "input_type": input_type,
                            "editable": editable,
                            "enabled": await item.is_enabled(),
                            "clickable": clickable,
                        }
                    )
                except Exception:
                    continue
        return BrowserSnapshot(self.url, self.title, text, tuple(result))

    async def locate(self, locator: Mapping[str, JsonValue]) -> Any:
        self._ensure()
        checked = _BrowserLocator.model_validate(locator)
        page = self.page
        if page is None:
            raise BrowserUnavailableError()
        if checked.by == "role":
            target = page.get_by_role(
                checked.role,
                name=checked.name,
                exact=checked.exact,
            )
        elif checked.by == "label":
            target = page.get_by_label(checked.value, exact=checked.exact)
        elif checked.by == "placeholder":
            target = page.get_by_placeholder(checked.value, exact=checked.exact)
        elif checked.by == "text":
            target = page.get_by_text(checked.value, exact=checked.exact)
        else:
            target = page.get_by_test_id(checked.value)
        count = await target.count()
        if checked.index is None:
            if count != 1:
                raise LocalActionError()
            return target
        if checked.index >= count:
            raise LocalActionError()
        return target.nth(checked.index)

    async def navigate(self, url: str) -> None:
        await self.page.goto(url, timeout=self.action * 1000)
        await self._refresh()

    async def fill(self, locator: object, value: str) -> None:
        await locator.fill(value, timeout=self.action * 1000)
        await self._refresh()

    async def type(self, locator: object, text: str) -> None:
        await locator.type(text, timeout=self.action * 1000)
        await self._refresh()

    async def click(self, locator: object) -> None:
        await locator.click(timeout=self.action * 1000)
        await self._refresh()

    async def state(self, locator: object) -> tuple[bool, bool, bool, bool, str]:
        if not await locator.evaluate("node => node.isConnected"):
            return False, False, False, False, ""
        visible, enabled = await locator.is_visible(), await locator.is_enabled()
        tag = await locator.get_attribute("type") or ""
        role = await locator.get_attribute("role") or ""
        node = await locator.evaluate("node => node.tagName.toLowerCase()")
        clickable = (
            role in {"button", "link", "checkbox", "radio"}
            or node in {"button", "a"}
            or tag.lower() in {"button", "submit", "checkbox", "radio"}
        )
        if not clickable:
            clickable = await locator.get_attribute("href") is not None
        editable = False if clickable else await locator.is_editable()
        return visible, enabled, editable, clickable, tag.lower()

    async def scroll(self, direction: str) -> None:
        await self.page.evaluate(
            "direction => window.scrollBy(0, direction === 'down' ? window.innerHeight : -window.innerHeight)",
            direction,
        )
        await self._refresh()

    async def back(self) -> bool:
        response = await self.page.go_back(timeout=self.action * 1000)
        await self._refresh()
        return response is not None

    async def reset(self) -> None:
        self.suppress_close = True
        try:
            if self.page and not self.page.is_closed():
                await self.page.close()
        finally:
            self.suppress_close = False
        self.page = await self.context.new_page()
        self._bind_page(self.page)
        self.url, self.title = self.page.url, ""

    async def wait_unavailable(self) -> None:
        await self.unavailable.wait()

    async def aclose(self) -> None:
        if self._close_task is None:
            self._closed = True
            self.unavailable.set()
            self._close_task = asyncio.create_task(self._aclose_owned())
        await _await_shared_cleanup(self._close_task)

    async def _aclose_owned(self) -> None:
        self.suppress_close = True
        tasks, self.tasks = self.tasks, set()
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), self.action
                )
            except TimeoutError:
                pass
        stages = []
        if self.page and not self.page.is_closed():
            stages.append(self.page.close)
        stages.extend((self.context.close, self.pw.stop))
        for close in stages:
            stage = asyncio.create_task(close())
            try:
                await asyncio.wait_for(
                    asyncio.gather(stage, return_exceptions=True), self.action
                )
            except TimeoutError:
                pass


_ARIA_ROOT = re.compile(
    r"^- (?P<role>[a-z][a-z0-9_-]*)(?: (?P<name>\"(?:\\.|[^\"\\])*\"|'(?:''|[^'])*'))?(?: \[[^\]\r\n]*\])*(?::[^\r\n]*)?$"
)


def _parse_root_aria_name(snapshot: str, expected_role: str) -> str:
    if "\r" in snapshot or "\n" in snapshot:
        raise ValueError("invalid aria snapshot")
    match = _ARIA_ROOT.fullmatch(snapshot.strip())
    if match is None or match.group("role") != expected_role:
        raise ValueError("invalid aria snapshot")
    quoted = match.group("name")
    if quoted is None:
        return ""
    if quoted.startswith('"'):
        value = json.loads(quoted)
    else:
        value = quoted[1:-1].replace("''", "'")
    if not isinstance(value, str):
        raise ValueError("invalid aria snapshot")
    return value

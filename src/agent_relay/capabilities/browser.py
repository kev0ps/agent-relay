"""Constrained, operator-enabled Chromium capability using a Playwright profile."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import urlsplit, urlunsplit

from ..protocol import (
    MAX_BROWSER_ELEMENT_ID_LENGTH,
    MAX_BROWSER_ELEMENT_VALUE_LENGTH,
    MAX_BROWSER_ELEMENTS,
    MAX_BROWSER_NAME_LENGTH,
    MAX_BROWSER_PAGE_TEXT_LENGTH,
    MAX_BROWSER_ROLE_LENGTH,
    MAX_BROWSER_TITLE_LENGTH,
    MAX_BROWSER_URL_LENGTH,
    BrowserBackInvoke,
    BrowserClickInvoke,
    BrowserFillInvoke,
    BrowserListTabsInvoke,
    BrowserNavigateInvoke,
    BrowserScrollInvoke,
    BrowserSnapshotInvoke,
    BrowserTypeInvoke,
    InvokeMessage,
    ToolName,
)


class LocalActionError(RuntimeError):
    """A browser operation failed without exposing backend details."""


class BrowserUnavailableError(LocalActionError):
    """The owned browser context is no longer safe to use."""


class BrowserStartupError(BrowserUnavailableError):
    """The configured persistent browser could not be launched safely."""


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


class BrowserHandle(Protocol): ...


@dataclass(frozen=True)
class BrowserElement:
    handle: BrowserHandle
    role: str
    name: str
    value: str | None


@dataclass(frozen=True)
class BrowserSnapshot:
    url: str
    title: str
    text: str
    elements: tuple[BrowserElement, ...]


class BrowserSession(Protocol):
    url: str
    title: str
    async def snapshot(self) -> BrowserSnapshot: ...
    async def navigate(self, url: str) -> None: ...
    async def fill(self, handle: BrowserHandle, value: str) -> None: ...
    async def type(self, handle: BrowserHandle, text: str) -> None: ...
    async def click(self, handle: BrowserHandle) -> None: ...
    async def scroll(self, direction: str) -> None: ...
    async def back(self) -> bool: ...
    async def state(self, handle: BrowserHandle) -> tuple[bool, bool, bool, bool, str]: ...
    async def dispose(self, handle: BrowserHandle) -> None: ...
    async def reset(self) -> None: ...
    async def wait_unavailable(self) -> None: ...
    async def aclose(self) -> None: ...
    def ensure(self, *, allow_blank: bool = False) -> None: ...


@dataclass
class _Record:
    session: BrowserSession
    handle: BrowserHandle
    editable: bool
    clickable: bool


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
    tools: frozenset[ToolName] = frozenset(
        {
            "browser.list_tabs",
            "browser.navigate",
            "browser.snapshot",
            "browser.fill",
            "browser.click",
            "browser.scroll",
            "browser.type",
            "browser.back",
        }
    )

    def __init__(
        self,
        user_data_dir: Path,
        allowed_origins: tuple[str, ...],
        *,
        headless: bool = False,
        startup_timeout_seconds: float = 15,
        action_timeout_seconds: float = 10,
        adapter_factory: Callable[..., BrowserSession | Awaitable[BrowserSession]] | None = None,
    ) -> None:
        if not isinstance(user_data_dir, Path) or not user_data_dir.is_absolute():
            raise ValueError("browser user data directory must be absolute")
        self._user_data_dir = user_data_dir
        self._origins = frozenset(normalize_origin(item) for item in allowed_origins)
        if not self._origins:
            raise ValueError("allowed origins required")
        self._headless = headless
        self._startup_timeout = startup_timeout_seconds
        self._action_timeout = action_timeout_seconds
        self._factory = adapter_factory or _RealSession.create
        self._session: BrowserSession | None = None
        self._records: dict[str, _Record] = {}
        self._tab_id = secrets.token_urlsafe(24)
        self._session_lock = asyncio.Lock()
        self._teardown_tasks: dict[int, asyncio.Task[None]] = {}
        self._teardown_records: dict[int, list[_Record]] = {}
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False

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

    async def invoke(self, message: InvokeMessage) -> dict[str, object]:
        owned_session = self._session
        try:
            if isinstance(message, BrowserListTabsInvoke):
                session = self._ready(allow_blank=True)
                result = {"tabs": [self._tab(session, allow_blank=True)]}
                self._ready(allow_blank=True)
                return result
            if isinstance(message, BrowserSnapshotInvoke):
                return await self._snapshot()
            if isinstance(message, BrowserNavigateInvoke):
                return await self._navigate(message.url)
            if isinstance(message, BrowserFillInvoke):
                return await self._fill(message.element_id, message.value)
            if isinstance(message, BrowserClickInvoke):
                return await self._click(message.element_id)
            if isinstance(message, BrowserScrollInvoke):
                return await self._scroll(message.direction)
            if isinstance(message, BrowserTypeInvoke):
                return await self._type(message.element_id, message.text)
            if isinstance(message, BrowserBackInvoke):
                return await self._back()
            raise LocalActionError()
        except asyncio.CancelledError:
            records, self._records = self._records, {}
            reset = owned_session is not None and self._session is owned_session
            cleanup = asyncio.create_task(
                self._cancel_invocation(list(records.values()), owned_session if reset else None)
            )
            await _await_shared_cleanup(cleanup)
            raise
        except LocalActionError:
            raise
        except Exception:
            raise LocalActionError() from None

    def _tab(self, session: BrowserSession, *, allow_blank: bool = False) -> dict[str, object]:
        if not (allow_blank and session.url == "about:blank"):
            self._check_origin(session.url)
        return {"tab_id": self._tab_id, "title": session.title[:MAX_BROWSER_TITLE_LENGTH], "url": session.url[:MAX_BROWSER_URL_LENGTH]}

    def _check_origin(self, url: str) -> None:
        try:
            allowed = origin_of_url(url) in self._origins
        except ValueError:
            allowed = False
        if not allowed:
            raise LocalActionError()

    async def _snapshot(self) -> dict[str, object]:
        session = self._ready()
        self._check_origin(session.url)
        await self._invalidate()
        snap = await asyncio.wait_for(session.snapshot(), self._action_timeout)
        self._check_origin(snap.url)
        elements: list[dict[str, object]] = []
        selected = snap.elements[:MAX_BROWSER_ELEMENTS]
        await asyncio.gather(
            *(session.dispose(item.handle) for item in snap.elements[MAX_BROWSER_ELEMENTS:]),
            return_exceptions=True,
        )
        for item in selected:
            try:
                visible, enabled, editable, _, input_type = await session.state(item.handle)
            except Exception:
                await self._dispose(session, item.handle)
                continue
            if not visible:
                await self._dispose(session, item.handle)
                continue
            element_id = secrets.token_urlsafe(24)[:MAX_BROWSER_ELEMENT_ID_LENGTH]
            semantic_clickable = item.role in {"button", "link", "checkbox", "radio"}
            self._records[element_id] = _Record(session, item.handle, editable and input_type not in {"password", "file"}, semantic_clickable)
            elements.append({"element_id": element_id, "role": item.role[:MAX_BROWSER_ROLE_LENGTH] or "unknown", "name": item.name[:MAX_BROWSER_NAME_LENGTH], "value": None if item.value is None else item.value[:MAX_BROWSER_ELEMENT_VALUE_LENGTH], "editable": editable and input_type not in {"password", "file"}, "enabled": enabled})
        result = {"tab_id": self._tab_id, "title": snap.title[:MAX_BROWSER_TITLE_LENGTH], "url": snap.url[:MAX_BROWSER_URL_LENGTH], "text": snap.text[:MAX_BROWSER_PAGE_TEXT_LENGTH], "elements": elements}
        self._ready()
        return result

    async def _navigate(self, url: str) -> dict[str, object]:
        self._check_origin(url)
        await self._invalidate()
        session = self._ready(allow_blank=True)
        await asyncio.wait_for(session.navigate(url), self._action_timeout)
        self._ready()
        self._check_origin(session.url)
        return self._action(None)

    async def _fill(self, element_id: str, value: str) -> dict[str, object]:
        record = self._records.get(element_id)
        if record is None or not record.editable:
            raise LocalActionError()
        session = self._ready()
        visible, enabled, editable, _, input_type = await session.state(record.handle)
        if not (visible and enabled and editable) or input_type in {"password", "file"}:
            raise LocalActionError()
        await asyncio.wait_for(session.fill(record.handle, value), self._action_timeout)
        self._ready()
        self._check_origin(session.url)
        return self._action(element_id)

    async def _type(self, element_id: str, text: str) -> dict[str, object]:
        record = self._records.get(element_id)
        if record is None or not record.editable:
            raise LocalActionError()
        session = self._ready()
        visible, enabled, editable, _, input_type = await session.state(record.handle)
        if not (visible and enabled and editable) or input_type in {"password", "file"}:
            raise LocalActionError()
        await asyncio.wait_for(session.type(record.handle, text), self._action_timeout)
        self._ready()
        self._check_origin(session.url)
        return self._action(element_id)

    async def _scroll(self, direction: str) -> dict[str, object]:
        if direction not in {"up", "down"}:
            raise LocalActionError()
        await self._invalidate()
        session = self._ready()
        await asyncio.wait_for(session.scroll(direction), self._action_timeout)
        self._ready()
        self._check_origin(session.url)
        return self._action(None)

    async def _back(self) -> dict[str, object]:
        await self._invalidate()
        session = self._ready()
        moved = await asyncio.wait_for(session.back(), self._action_timeout)
        if not moved:
            raise LocalActionError()
        self._ready()
        self._check_origin(session.url)
        return self._action(None)

    async def _click(self, element_id: str) -> dict[str, object]:
        record = self._records.pop(element_id, None)
        if record is None:
            raise LocalActionError()
        try:
            if not record.clickable:
                raise LocalActionError()
            session = self._ready()
            if session is not record.session:
                raise BrowserUnavailableError()
            visible, enabled, _, clickable, _ = await session.state(record.handle)
            if not (visible and enabled and clickable):
                raise LocalActionError()
            await asyncio.wait_for(session.click(record.handle), self._action_timeout)
            self._ready()
            self._check_origin(session.url)
            await self._invalidate()
            return self._action(element_id)
        finally:
            await self._dispose_shielded(record.session, record.handle)

    def _action(self, element_id: str | None) -> dict[str, object]:
        session = self._ready()
        return self._tab(session) | {"element_id": element_id, "success": True}

    async def _invalidate(self) -> None:
        records, self._records = self._records, {}
        cleanup = asyncio.create_task(self._dispose_records(list(records.values())))
        await _await_shared_cleanup(cleanup)

    async def _dispose_records(self, records: list[_Record]) -> None:
        await asyncio.gather(
            *(self._dispose(record.session, record.handle) for record in records),
            return_exceptions=True,
        )

    async def _cancel_invocation(
        self, records: list[_Record], session: BrowserSession | None
    ) -> None:
        await self._dispose_records(records)
        if session is not None:
            try:
                await asyncio.wait_for(session.reset(), self._action_timeout)
            except Exception:
                pass

    async def _dispose(self, session: BrowserSession, handle: BrowserHandle) -> None:
        try:
            await asyncio.wait_for(session.dispose(handle), self._action_timeout)
        except Exception:
            pass

    async def _dispose_shielded(self, session: BrowserSession, handle: BrowserHandle) -> None:
        cleanup = asyncio.create_task(self._dispose(session, handle))
        await _await_shared_cleanup(cleanup)

    async def wait_unavailable(self) -> None:
        session = self._session
        if session is None:
            raise BrowserUnavailableError()
        await session.wait_unavailable()
        async with self._session_lock:
            if self._session is session:
                self._session = None
                records, self._records = self._records, {}
                self._schedule_teardown(session, list(records.values()))

    def _schedule_teardown(self, session: BrowserSession, records: list[_Record]) -> None:
        key = id(session)
        self._teardown_records.setdefault(key, []).extend(records)
        if key in self._teardown_tasks:
            return
        task = asyncio.create_task(self._close_session(session, key))
        self._teardown_tasks[key] = task

        def completed(done: asyncio.Task[None]) -> None:
            if self._teardown_tasks.get(key) is done:
                self._teardown_tasks.pop(key, None)
                self._teardown_records.pop(key, None)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(completed)

    async def _drain_teardowns(self) -> None:
        while self._teardown_tasks:
            active = tuple(self._teardown_tasks.items())
            await asyncio.gather(*(asyncio.shield(task) for _, task in active))
            for key, task in active:
                if self._teardown_tasks.get(key) is task and task.done():
                    self._teardown_tasks.pop(key, None)
                    self._teardown_records.pop(key, None)

    async def _close_session(self, session: BrowserSession, key: int) -> None:
        while records := self._teardown_records.get(key):
            batch, records[:] = records[:], []
            await asyncio.gather(
                *(self._dispose(record.session, record.handle) for record in batch),
                return_exceptions=True,
            )
        close = asyncio.create_task(session.aclose())
        try:
            await asyncio.wait_for(
                asyncio.gather(close, return_exceptions=True),
                max(self._startup_timeout, self._action_timeout * 6),
            )
        except TimeoutError:
            pass

    async def aclose(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._aclose_owned())
        await _await_shared_cleanup(self._close_task)

    async def _aclose_owned(self) -> None:
        async with self._session_lock:
            self._closed = True
            records, self._records = self._records, {}
            session, self._session = self._session, None
        if session:
            self._schedule_teardown(session, list(records.values()))
        else:
            await self._dispose_records(list(records.values()))
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
    ) -> _RealSession:
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
            obj = cls(pw, context, origins, action)
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
    ) -> None:
        self.pw, self.context = pw, context
        self.origins, self.action = origins, action
        self.page = None
        self.url, self.title = "about:blank", ""
        self.unavailable = asyncio.Event()
        self.tasks: set[asyncio.Task[object]] = set()
        self.suppress_close = False
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False

    async def _arm(self) -> None:
        async def route_handler(route: object) -> None:
            try:
                if origin_of_url(route.request.url) in self.origins:
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
                if frame.parent_frame is not None or origin_of_url(frame.url) not in self.origins:
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
        if self_url != "about:blank" and origin_of_url(self_url) not in self.origins:
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
        result: list[BrowserElement] = []
        for role, editable, clickable in (("textbox", True, False), ("button", False, True), ("link", False, True), ("checkbox", False, True), ("radio", False, True), ("combobox", True, False)):
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
                handle = None
                try:
                    if not await item.is_visible():
                        continue
                    handle = await item.element_handle()
                    if handle:
                        try:
                            aria = await item.aria_snapshot(
                                timeout=self.action * 1000, depth=0
                            )
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
                        value = None
                        if editable:
                            value = await item.evaluate(
                                "(node, limit) => ('value' in node) ? String(node.value).slice(0, limit) : null",
                                MAX_BROWSER_ELEMENT_VALUE_LENGTH,
                            )
                        result.append(BrowserElement(handle, role, name, value))
                except Exception:
                    if handle is not None:
                        try:
                            await handle.dispose()
                        except Exception:
                            pass
        return BrowserSnapshot(self.url, self.title, text, tuple(result))

    async def navigate(self, url: str) -> None:
        await self.page.goto(url, timeout=self.action * 1000)
        await self._refresh()

    async def fill(self, handle: object, value: str) -> None:
        await handle.fill(value, timeout=self.action * 1000)
        await self._refresh()

    async def type(self, handle: object, text: str) -> None:
        await handle.type(text, timeout=self.action * 1000)
        await self._refresh()

    async def click(self, handle: object) -> None:
        await handle.click(timeout=self.action * 1000)
        await self._refresh()

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

    async def state(self, handle: Any) -> tuple[bool, bool, bool, bool, str]:
        if not await handle.evaluate("node => node.isConnected"):
            return False, False, False, False, ""
        visible, enabled = await handle.is_visible(), await handle.is_enabled()
        tag = await handle.get_attribute("type") or ""
        role = await handle.get_attribute("role") or ""
        node = await handle.evaluate("node => node.tagName.toLowerCase()")
        clickable = (
            role in {"button", "link", "checkbox", "radio"}
            or node in {"button", "a"}
            or tag.lower() in {"button", "submit", "checkbox", "radio"}
        )
        if not clickable:
            clickable = await handle.get_attribute("href") is not None
        editable = False if clickable else await handle.is_editable()
        return visible, enabled, editable, clickable, tag.lower()

    async def dispose(self, handle: object) -> None:
        await handle.dispose()

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

    async def wait_unavailable(self) -> None: await self.unavailable.wait()

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

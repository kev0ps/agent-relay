from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_relay.capabilities.browser import (
    BrowserCapability,
    BrowserElement,
    BrowserSnapshot,
    BrowserStartupError,
    BrowserUnavailableError,
    LocalActionError,
    _parse_root_aria_name,
    _RealSession,
)
from agent_relay.protocol import (
    MAX_BROWSER_ELEMENT_VALUE_LENGTH,
    MAX_BROWSER_ELEMENTS,
    MAX_BROWSER_NAME_LENGTH,
    MAX_BROWSER_PAGE_TEXT_LENGTH,
    BrowserBackInvoke,
    BrowserClickInvoke,
    BrowserFillInvoke,
    BrowserListTabsInvoke,
    BrowserNavigateInvoke,
    BrowserScrollInvoke,
    BrowserSnapshotInvoke,
    BrowserTypeInvoke,
)


@dataclass
class Handle:
    visible: bool = True
    enabled: bool = True
    editable: bool = False
    clickable: bool = False
    input_type: str = "text"
    disposed: bool = False


class FakeSession:
    def __init__(self) -> None:
        self.url = "http://127.0.0.1:8899/"
        self.title = "fixture"
        self.handles = [
            Handle(editable=True),
            Handle(clickable=True),
            Handle(editable=True, input_type="password"),
        ]
        self.navigate_to: str | None = None
        self.filled: list[tuple[Handle, str]] = []
        self.typed: list[tuple[Handle, str]] = []
        self.clicked: list[Handle] = []
        self.scrolled: list[str] = []
        self.back_calls = 0
        self.closed = 0
        self.unavailable = asyncio.Event()

    async def snapshot(self) -> BrowserSnapshot:
        return BrowserSnapshot(
            url=self.url,
            title=self.title,
            text="x" * 5000,
            elements=tuple(
                BrowserElement(h, "textbox" if h.editable else "button", "n" * 200, "v" * 300)
                for h in self.handles * 6
            ),
        )

    async def navigate(self, url: str) -> None:
        self.navigate_to = url
        self.url = url

    async def fill(self, handle: Handle, value: str) -> None:
        self.filled.append((handle, value))

    async def type(self, handle: Handle, text: str) -> None:
        self.typed.append((handle, text))

    async def click(self, handle: Handle) -> None:
        self.clicked.append(handle)

    async def scroll(self, direction: str) -> None:
        self.scrolled.append(direction)

    async def back(self) -> bool:
        self.back_calls += 1
        return True

    async def state(self, handle: Handle) -> tuple[bool, bool, bool, bool, str]:
        return handle.visible, handle.enabled, handle.editable, handle.clickable, handle.input_type

    async def dispose(self, handle: Handle) -> None:
        handle.disposed = True

    async def reset(self) -> None:
        self.url = "http://127.0.0.1:8899/"

    async def wait_unavailable(self) -> None:
        await self.unavailable.wait()

    async def aclose(self) -> None:
        self.closed += 1

    def ensure(self, *, allow_blank: bool = False) -> None:
        if not allow_blank and self.url == "about:blank":
            raise LocalActionError()


def msg(kind: str, **values: str):
    classes = {
        "list": (BrowserListTabsInvoke, "browser.list_tabs"),
        "navigate": (BrowserNavigateInvoke, "browser.navigate"),
        "snapshot": (BrowserSnapshotInvoke, "browser.snapshot"),
        "fill": (BrowserFillInvoke, "browser.fill"),
        "click": (BrowserClickInvoke, "browser.click"),
        "scroll": (BrowserScrollInvoke, "browser.scroll"),
        "type": (BrowserTypeInvoke, "browser.type"),
        "back": (BrowserBackInvoke, "browser.back"),
    }
    cls, tool = classes[kind]
    return cls(version=1, type="invoke", request_id="r", tool=tool, **values)


def test_browser_startup_failures_are_terminal_and_sanitized() -> None:
    async def fail(*_args: object) -> FakeSession:
        raise RuntimeError("profile lock: /personal/path")

    async def scenario() -> None:
        capability = BrowserCapability(
            Path.cwd() / "browser-profile",
            ("http://127.0.0.1:8899",),
            adapter_factory=fail,
        )
        with pytest.raises(BrowserStartupError) as error:
            await capability.start()
        assert str(error.value) == ""

    asyncio.run(scenario())



def test_browser_bounds_random_handles_and_exact_pinned_actions() -> None:
    async def scenario() -> None:
        session = FakeSession()
        capability = BrowserCapability(
            Path.cwd() / "browser-profile", ("http://127.0.0.1:8899",),
            adapter_factory=lambda *_: session,
        )
        await capability.start()
        tabs = await capability.invoke(msg("list"))
        assert len(tabs["tabs"]) == 1
        first = await capability.invoke(msg("snapshot"))
        assert len(first["text"]) == MAX_BROWSER_PAGE_TEXT_LENGTH
        assert len(first["elements"]) == MAX_BROWSER_ELEMENTS
        old_id = first["elements"][0]["element_id"]
        second = await capability.invoke(msg("snapshot"))
        assert old_id not in {item["element_id"] for item in second["elements"]}
        editable_id = second["elements"][0]["element_id"]
        button_id = second["elements"][1]["element_id"]
        await capability.invoke(msg("fill", element_id=editable_id, value="hello"))
        await capability.invoke(msg("type", element_id=editable_id, text="typed"))
        await capability.invoke(msg("scroll", direction="down"))
        await capability.invoke(msg("back"))
        refreshed = await capability.invoke(msg("snapshot"))
        button_id = refreshed["elements"][1]["element_id"]
        await capability.invoke(msg("click", element_id=button_id))
        assert session.filled == [(session.handles[0], "hello")]
        assert session.typed == [(session.handles[0], "typed")]
        assert session.scrolled == ["down"]
        assert session.back_calls == 1
        assert session.clicked == [session.handles[1]]
        with pytest.raises(LocalActionError):
            await capability.invoke(msg("click", element_id=button_id))
        await capability.aclose()
        await capability.aclose()
        assert session.closed == 1

    asyncio.run(scenario())


def test_click_revalidation_rejects_element_that_is_no_longer_clickable() -> None:
    async def scenario() -> None:
        session = FakeSession()
        capability = BrowserCapability(
            Path.cwd() / "browser-profile",
            ("http://127.0.0.1:8899",),
            adapter_factory=lambda *_: session,
        )
        await capability.start()
        page = await capability.invoke(msg("snapshot"))
        button_id = page["elements"][1]["element_id"]
        button = session.handles[1]
        button.clickable = False

        with pytest.raises(LocalActionError):
            await capability.invoke(msg("click", element_id=button_id))

        assert session.clicked == []
        assert button.disposed

    asyncio.run(scenario())


def test_navigation_origin_and_stale_or_forbidden_elements_are_rejected() -> None:
    async def scenario() -> None:
        session = FakeSession()
        capability = BrowserCapability(
            Path.cwd() / "browser-profile", ("HTTP://127.0.0.1:8899",),
            adapter_factory=lambda *_: session,
        )
        await capability.start()
        page = await capability.invoke(msg("snapshot"))
        editable_id = page["elements"][0]["element_id"]
        password_id = page["elements"][2]["element_id"]
        with pytest.raises(LocalActionError):
            await capability.invoke(msg("fill", element_id=password_id, value="secret"))
        with pytest.raises(LocalActionError):
            await capability.invoke(msg("navigate", url="https://example.com/"))
        await capability.invoke(msg("navigate", url="http://127.0.0.1:8899/next"))
        with pytest.raises(LocalActionError):
            await capability.invoke(msg("fill", element_id=editable_id, value="stale"))

    asyncio.run(scenario())


def test_final_navigation_origin_and_cancellation_reset_are_enforced() -> None:
    async def scenario() -> None:
        session = FakeSession()

        async def redirected(url: str) -> None:
            session.url = "http://127.0.0.1:9999/escape"

        session.navigate = redirected  # type: ignore[method-assign]
        capability = BrowserCapability(
            Path.cwd() / "browser-profile", ("http://127.0.0.1:8899",),
            adapter_factory=lambda *_: session,
        )
        await capability.start()
        with pytest.raises(LocalActionError):
            await capability.invoke(msg("navigate", url="http://127.0.0.1:8899/go"))

        blocker = asyncio.Event()

        async def blocked(url: str) -> None:
            await blocker.wait()

        session.navigate = blocked  # type: ignore[method-assign]
        task = asyncio.create_task(capability.invoke(msg("navigate", url="http://127.0.0.1:8899/go")))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.url == "http://127.0.0.1:8899/"

    asyncio.run(scenario())


def test_fresh_about_blank_tab_can_be_listed_but_not_read() -> None:
    async def scenario() -> None:
        session = FakeSession()
        session.url = "about:blank"
        session.title = ""
        capability = BrowserCapability(
            Path.cwd() / "browser-profile",
            ("http://127.0.0.1:8899",),
            adapter_factory=lambda *_: session,
        )
        await capability.start()
        assert await capability.invoke(msg("list")) == {
            "tabs": [{"tab_id": capability._tab_id, "title": "", "url": "about:blank"}]
        }
        with pytest.raises(LocalActionError):
            await capability.invoke(msg("snapshot"))

    asyncio.run(scenario())


def test_fresh_about_blank_tab_can_navigate_to_allowed_origin() -> None:
    async def scenario() -> None:
        session = FakeSession()
        session.url = "about:blank"
        session.title = ""
        capability = BrowserCapability(
            Path.cwd() / "browser-profile",
            ("http://127.0.0.1:8899",),
            adapter_factory=lambda *_: session,
        )
        await capability.start()
        await capability.invoke(msg("list"))
        result = await capability.invoke(
            msg("navigate", url="http://127.0.0.1:8899/first")
        )
        assert result["success"] is True
        assert result["url"] == "http://127.0.0.1:8899/first"

    asyncio.run(scenario())


def test_cancelled_first_navigation_from_blank_resets_and_preserves_cancellation() -> None:
    async def scenario() -> None:
        session = FakeSession()
        session.url = "about:blank"
        reset_calls = 0
        blocked = asyncio.Event()

        async def navigate(_: str) -> None:
            await blocked.wait()

        async def reset() -> None:
            nonlocal reset_calls
            reset_calls += 1

        session.navigate = navigate  # type: ignore[method-assign]
        session.reset = reset  # type: ignore[method-assign]
        capability = BrowserCapability(
            Path.cwd() / "browser-profile",
            ("http://127.0.0.1:8899",),
            adapter_factory=lambda *_: session,
        )
        await capability.start()
        task = asyncio.create_task(
            capability.invoke(msg("navigate", url="http://127.0.0.1:8899/first"))
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert reset_calls == 1

    asyncio.run(scenario())


def test_cancelled_invoke_defers_repeated_cancellation_until_owned_cleanup() -> None:
    class BlockingDisposeSession(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.dispose_started = asyncio.Event()
            self.dispose_allowed = asyncio.Event()
            self.dispose_calls: list[Handle] = []
            self.reset_calls = 0

        async def snapshot(self) -> BrowserSnapshot:
            return BrowserSnapshot(
                self.url,
                self.title,
                "fixture",
                tuple(
                    BrowserElement(handle, "textbox", "field", None)
                    for handle in self.handles
                ),
            )

        async def fill(self, handle: Handle, value: str) -> None:
            await asyncio.Event().wait()

        async def dispose(self, handle: Handle) -> None:
            self.dispose_calls.append(handle)
            self.dispose_started.set()
            await self.dispose_allowed.wait()
            handle.disposed = True

        async def reset(self) -> None:
            self.reset_calls += 1

    async def scenario() -> None:
        session = BlockingDisposeSession()
        capability = BrowserCapability(
            Path.cwd() / "browser-profile", ("http://127.0.0.1:8899",),
            action_timeout_seconds=1, adapter_factory=lambda *_: session,
        )
        await capability.start()
        page = await capability.invoke(msg("snapshot"))
        records = list(capability._records.values())
        task = asyncio.create_task(capability.invoke(
            msg("fill", element_id=page["elements"][0]["element_id"], value="hello")
        ))
        await asyncio.sleep(0)
        task.cancel()
        await session.dispose_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        session.dispose_allowed.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.reset_calls == 1
        assert len(session.dispose_calls) == len(records)
        assert all(record.handle.disposed for record in records)

    asyncio.run(scenario())


def test_invalidate_defers_cancellation_until_detached_records_are_disposed() -> None:
    class BlockingDisposeSession(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.dispose_started = asyncio.Event()
            self.dispose_allowed = asyncio.Event()

        async def snapshot(self) -> BrowserSnapshot:
            return BrowserSnapshot(
                self.url,
                self.title,
                "fixture",
                tuple(
                    BrowserElement(handle, "textbox", "field", None)
                    for handle in self.handles
                ),
            )

        async def dispose(self, handle: Handle) -> None:
            self.dispose_started.set()
            await self.dispose_allowed.wait()
            handle.disposed = True

    async def scenario() -> None:
        session = BlockingDisposeSession()
        capability = BrowserCapability(
            Path.cwd() / "browser-profile", ("http://127.0.0.1:8899",),
            action_timeout_seconds=1, adapter_factory=lambda *_: session,
        )
        await capability.start()
        page = await capability.invoke(msg("snapshot"))
        records = [capability._records[item["element_id"]] for item in page["elements"]]
        task = asyncio.create_task(capability._invalidate())
        await session.dispose_started.wait()
        assert not capability._records
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        session.dispose_allowed.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(record.handle.disposed for record in records)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["state", "click", "timeout", "cancel"])
def test_consumed_click_handle_is_disposed_on_every_exit(failure: str) -> None:
    async def scenario() -> None:
        session = FakeSession()
        capability = BrowserCapability(
            Path.cwd() / "browser-profile",
            ("http://127.0.0.1:8899",),
            action_timeout_seconds=0.01,
            adapter_factory=lambda *_: session,
        )
        await capability.start()
        page = await capability.invoke(msg("snapshot"))
        element_id = page["elements"][1]["element_id"]
        handle = session.handles[1]
        blocker = asyncio.Event()
        if failure == "state":
            handle.visible = False
        elif failure == "click":
            async def broken(_: Handle) -> None:
                raise RuntimeError("playwright failed")
            session.click = broken  # type: ignore[method-assign]
        elif failure in {"timeout", "cancel"}:
            async def blocked(_: Handle) -> None:
                await blocker.wait()
            session.click = blocked  # type: ignore[method-assign]
        task = asyncio.create_task(capability.invoke(msg("click", element_id=element_id)))
        if failure == "cancel":
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(LocalActionError):
                await task
        assert handle.disposed

    asyncio.run(scenario())


def test_non_clickable_semantic_element_consumes_id_and_disposes_handle() -> None:
    async def scenario() -> None:
        session = FakeSession()
        capability = BrowserCapability(
            Path.cwd() / "browser-profile",
            ("http://127.0.0.1:8899",),
            adapter_factory=lambda *_: session,
        )
        await capability.start()
        page = await capability.invoke(msg("snapshot"))
        element_id = page["elements"][0]["element_id"]
        handle = session.handles[0]
        with pytest.raises(LocalActionError):
            await capability.invoke(msg("click", element_id=element_id))
        assert handle.disposed
        with pytest.raises(LocalActionError):
            await capability.invoke(msg("click", element_id=element_id))

    asyncio.run(scenario())


class Emitter:
    def __init__(self) -> None:
        self.handlers: dict[str, list[object]] = {}

    def on(self, event: str, callback: object) -> None:
        self.handlers.setdefault(event, []).append(callback)

    def emit(self, event: str, value: object) -> None:
        for callback in self.handlers[event]:
            callback(value)  # type: ignore[operator]


def test_playwright_object_event_callback_signatures_mark_unavailable() -> None:
    class Page(Emitter):
        pass

    context = Emitter()
    session = _RealSession(object(), context, frozenset(), 0.1)
    page = Page()
    session._bind_page(page)
    page.emit("crash", page)
    assert session.unavailable.is_set()

    session.unavailable.clear()
    session._arm_browser_events()
    context.emit("close", context)
    assert session.unavailable.is_set()


@pytest.mark.parametrize(
    "event", ["dialog", "download", "filechooser", "popup", "websocket", "crash", "close"]
)
def test_page_policy_events_mark_fatal_before_cleanup(event: str) -> None:
    class Page(Emitter):
        pass

    class EventObject:
        async def dismiss(self) -> None:
            assert session.unavailable.is_set()

        async def cancel(self) -> None:
            assert session.unavailable.is_set()

        async def close(self) -> None:
            assert session.unavailable.is_set()

    async def scenario() -> None:
        nonlocal session
        session = _RealSession(object(), object(), frozenset(), 0.1)
        page = Page()
        session._bind_page(page)
        page.emit(event, EventObject())
        assert session.unavailable.is_set()
        await asyncio.sleep(0)

    session: _RealSession
    asyncio.run(scenario())


def test_real_session_close_is_concurrent_idempotent_and_continues_after_failures() -> None:
    class Stage:
        def __init__(self, *, fail: bool = False) -> None:
            self.calls = 0
            self.fail = fail

        async def close(self) -> None:
            self.calls += 1
            if self.fail:
                raise RuntimeError("stage failed")

    class Page(Stage):
        def is_closed(self) -> bool:
            return False

    class Browser(Stage):
        pass

    class Playwright:
        def __init__(self) -> None:
            self.calls = 0

        async def stop(self) -> None:
            self.calls += 1
            raise RuntimeError("stop failed")

    async def scenario() -> None:
        page = Page(fail=True)
        context = Stage(fail=True)
        pw = Playwright()
        session = _RealSession(pw, context, frozenset(), 0.1)
        session.page = page
        await asyncio.gather(session.aclose(), session.aclose())
        assert (page.calls, context.calls, pw.calls) == (1, 1, 1)

    asyncio.run(scenario())


def test_real_session_close_continues_after_stage_timeout() -> None:
    class Stage:
        def __init__(self, blocked: bool = False) -> None:
            self.calls = 0
            self.blocked = blocked

        async def close(self) -> None:
            self.calls += 1
            if self.blocked:
                await asyncio.Event().wait()

    class Page(Stage):
        def is_closed(self) -> bool:
            return False

    class Playwright:
        def __init__(self) -> None:
            self.calls = 0

        async def stop(self) -> None:
            self.calls += 1

    async def scenario() -> None:
        page, context, pw = Page(True), Stage(), Playwright()
        session = _RealSession(pw, context, frozenset(), 0.01)
        session.page = page
        await session.aclose()
        assert (page.calls, context.calls, pw.calls) == (1, 1, 1)

    asyncio.run(scenario())


def test_real_session_close_defers_external_cancellation_until_cleanup_finishes() -> None:
    class Stage:
        def __init__(self) -> None:
            self.calls = 0

        async def close(self) -> None:
            self.calls += 1

    class Page:
        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        def is_closed(self) -> bool:
            return False

        async def close(self) -> None:
            self.calls += 1
            self.started.set()
            await self.release.wait()

    async def scenario() -> None:
        class Playwright:
            async def stop(self) -> None:
                return None

        page, context = Page(), Stage()
        pw = Playwright()
        pw.calls = 0
        original_stop = pw.stop

        async def counted_stop() -> None:
            pw.calls += 1
            await original_stop()

        pw.stop = counted_stop  # type: ignore[method-assign]
        session = _RealSession(pw, context, frozenset(), 1)
        session.page = page
        first = asyncio.create_task(session.aclose())
        await page.started.wait()
        second = asyncio.create_task(session.aclose())
        first.cancel()
        await asyncio.sleep(0)
        assert not first.done()
        assert not second.done()
        page.release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        await session.aclose()
        assert (page.calls, context.calls, pw.calls) == (1, 1, 1)
        assert session._close_task is not None and session._close_task.done()

    asyncio.run(scenario())


def test_wait_unavailable_and_close_detach_same_session_once() -> None:
    async def scenario() -> None:
        session = FakeSession()
        capability = BrowserCapability(
            Path.cwd() / "browser-profile",
            ("http://127.0.0.1:8899",),
            adapter_factory=lambda *_: session,
        )
        await capability.start()
        waiter = asyncio.create_task(capability.wait_unavailable())
        session.unavailable.set()
        await asyncio.gather(waiter, capability.aclose())
        assert session.closed == 1

    asyncio.run(scenario())


def test_wait_unavailable_returns_before_close_and_reconnect_drains_once() -> None:
    class BlockingCloseSession(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.close_started = asyncio.Event()
            self.close_allowed = asyncio.Event()

        async def aclose(self) -> None:
            self.closed += 1
            self.close_started.set()
            await self.close_allowed.wait()

    async def scenario() -> None:
        first = BlockingCloseSession()
        second = FakeSession()
        sessions = iter((first, second))
        capability = BrowserCapability(
            Path.cwd() / "browser-profile",
            ("http://127.0.0.1:8899",),
            adapter_factory=lambda *_: next(sessions),
        )
        await capability.start()
        first.unavailable.set()
        await asyncio.wait_for(capability.wait_unavailable(), 0.1)
        await asyncio.wait_for(first.close_started.wait(), 0.1)
        reconnect = asyncio.create_task(capability.start())
        await asyncio.sleep(0)
        assert not reconnect.done()
        first.close_allowed.set()
        await reconnect
        await capability.aclose()
        assert first.closed == 1
        assert second.closed == 1
        assert not capability._teardown_tasks

    asyncio.run(scenario())


def test_cancelled_reconnect_does_not_cancel_owned_loss_teardown() -> None:
    class BlockingTeardownSession(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.dispose_started = asyncio.Event()
            self.dispose_allowed = asyncio.Event()

        async def snapshot(self) -> BrowserSnapshot:
            return BrowserSnapshot(
                self.url,
                self.title,
                "fixture",
                tuple(
                    BrowserElement(handle, "textbox", "field", None)
                    for handle in self.handles
                ),
            )

        async def dispose(self, handle: Handle) -> None:
            self.dispose_started.set()
            await self.dispose_allowed.wait()
            handle.disposed = True

    async def scenario() -> None:
        session = BlockingTeardownSession()
        capability = BrowserCapability(
            Path.cwd() / "browser-profile", ("http://127.0.0.1:8899",),
            action_timeout_seconds=1, adapter_factory=lambda *_: session,
        )
        await capability.start()
        page = await capability.invoke(msg("snapshot"))
        records = [capability._records[item["element_id"]] for item in page["elements"]]
        session.unavailable.set()
        await capability.wait_unavailable()
        await session.dispose_started.wait()
        reconnect = asyncio.create_task(capability.start())
        await asyncio.sleep(0)
        reconnect.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reconnect
        teardown = next(iter(capability._teardown_tasks.values()))
        assert not teardown.cancelled()
        session.dispose_allowed.set()
        await capability.aclose()
        assert teardown.done() and not teardown.cancelled()
        assert all(record.handle.disposed for record in records)
        assert not capability._teardown_tasks

    asyncio.run(scenario())


def test_final_close_prevents_reconnect_waiting_on_old_session_teardown() -> None:
    class BlockingCloseSession(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.close_started = asyncio.Event()
            self.close_allowed = asyncio.Event()

        async def aclose(self) -> None:
            self.closed += 1
            self.close_started.set()
            await self.close_allowed.wait()

    async def scenario() -> None:
        first = BlockingCloseSession()
        factory_calls = 0

        def factory(*_: object) -> FakeSession:
            nonlocal factory_calls
            factory_calls += 1
            return first if factory_calls == 1 else FakeSession()

        capability = BrowserCapability(
            Path.cwd() / "browser-profile",
            ("http://127.0.0.1:8899",),
            adapter_factory=factory,
        )
        await capability.start()
        first.unavailable.set()
        await asyncio.wait_for(capability.wait_unavailable(), 0.1)
        await asyncio.wait_for(first.close_started.wait(), 0.1)
        reconnect = asyncio.create_task(capability.start())
        await asyncio.sleep(0)
        assert not reconnect.done()
        close = asyncio.create_task(capability.aclose())
        first.close_allowed.set()
        with pytest.raises(BrowserUnavailableError):
            await reconnect
        await close
        assert factory_calls == 1
        assert not capability._teardown_tasks

    asyncio.run(scenario())


def test_loss_disposes_all_pinned_handles_before_blocked_session_close() -> None:
    class BlockingCloseSession(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.close_started = asyncio.Event()
            self.close_allowed = asyncio.Event()

        async def aclose(self) -> None:
            self.closed += 1
            self.close_started.set()
            await self.close_allowed.wait()

    async def scenario() -> None:
        session = BlockingCloseSession()
        capability = BrowserCapability(
            Path.cwd() / "browser-profile", ("http://127.0.0.1:8899",),
            adapter_factory=lambda *_: session,
        )
        await capability.start()
        page = await capability.invoke(msg("snapshot"))
        pinned = [capability._records[item["element_id"]].handle for item in page["elements"]]
        session.unavailable.set()
        await asyncio.wait_for(capability.wait_unavailable(), 0.1)
        await asyncio.wait_for(session.close_started.wait(), 0.1)
        for _ in range(10):
            if all(handle.disposed for handle in pinned):
                break
            await asyncio.sleep(0)
        assert all(handle.disposed for handle in pinned)
        assert not hasattr(capability, "_closed_sessions")
        session.close_allowed.set()
        await capability.aclose()
        assert not capability._teardown_tasks

    asyncio.run(scenario())


def test_browser_close_defers_cancellation_and_shares_owned_teardown() -> None:
    class BlockingCloseSession(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.close_started = asyncio.Event()
            self.close_allowed = asyncio.Event()

        async def aclose(self) -> None:
            self.closed += 1
            self.close_started.set()
            await self.close_allowed.wait()

    async def scenario() -> None:
        session = BlockingCloseSession()
        capability = BrowserCapability(
            Path.cwd() / "browser-profile", ("http://127.0.0.1:8899",),
            adapter_factory=lambda *_: session,
        )
        await capability.start()
        first = asyncio.create_task(capability.aclose())
        await session.close_started.wait()
        second = asyncio.create_task(capability.aclose())
        first.cancel()
        await asyncio.sleep(0)
        assert not first.done()
        assert not second.done()
        session.close_allowed.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        assert session.closed == 1
        assert not capability._teardown_tasks

    asyncio.run(scenario())


def test_real_snapshot_uses_native_aria_names_bounds_values_and_skips_bad_snapshot() -> None:
    class Handle:
        def __init__(self) -> None:
            self.disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    class Item:
        def __init__(
            self,
            snapshot: str,
            value: str,
            *,
            role: str = "textbox",
            fallback_name: str = "",
            value_error: bool = False,
        ) -> None:
            self.snapshot = snapshot
            self.value = value
            self.role = role
            self.fallback_name = fallback_name
            self.value_error = value_error
            self.handle = Handle()

        async def is_visible(self) -> bool:
            return True

        async def element_handle(self) -> Handle:
            return self.handle

        async def aria_snapshot(self, **kwargs: object) -> str:
            assert kwargs == {"timeout": 1000, "depth": 0}
            return self.snapshot

        async def evaluate(self, expression: str, limit: int) -> str:
            if "'value' in node" in expression:
                if self.value_error:
                    raise RuntimeError("non-editable value must not be read")
                assert limit == MAX_BROWSER_ELEMENT_VALUE_LENGTH
                return self.value[:limit]
            assert "textContent" in expression
            assert limit == MAX_BROWSER_NAME_LENGTH
            return self.fallback_name[:limit]

    class Locator:
        def __init__(self, items: list[Item]) -> None:
            self.items = items

        async def count(self) -> int:
            return len(self.items)

        def nth(self, index: int) -> Item:
            return self.items[index]

    class Page:
        url = "http://127.0.0.1:8899/"

        def __init__(self, items: list[Item]) -> None:
            self.items = items

        def get_by_role(self, role: str) -> Locator:
            return Locator(
                [] if role == "button" else [item for item in self.items if item.role == role]
            )

        def locator(self, selector: str) -> object:
            if selector == (
                'button, input[type="button"], input[type="submit"], [role="button"]'
            ):
                return Locator([item for item in self.items if item.role == "button"])
            assert selector == "body"

            class Body:
                async def evaluate(self, expression: str, limit: int) -> str:
                    return "body"

            return Body()

    async def scenario() -> None:
        labelledby = Item('- textbox "Account owner": account value', "v" * (MAX_BROWSER_ELEMENT_VALUE_LENGTH + 10))
        label = Item("- textbox 'Native label': native value", "short")
        bounded = Item(f'- textbox "{"n" * (MAX_BROWSER_NAME_LENGTH + 10)}"', "short")
        malformed = Item("not a root snapshot", "ignored")
        recovered_button = Item(
            "not a root snapshot",
            "ignored",
            role="button",
            fallback_name="Submit",
            value_error=True,
        )
        session = _RealSession(object(), object(), frozenset(), 1)
        session.page = Page([labelledby, label, bounded, malformed, recovered_button])
        session._refresh = lambda: asyncio.sleep(0)  # type: ignore[method-assign]
        session.url, session.title = session.page.url, "fixture"
        snapshot = await session.snapshot()
        assert [element.name for element in snapshot.elements] == [
            "Account owner", "Native label", "n" * MAX_BROWSER_NAME_LENGTH, "Submit",
        ]
        assert snapshot.elements[0].value == "v" * MAX_BROWSER_ELEMENT_VALUE_LENGTH
        assert all(len(element.name) <= MAX_BROWSER_NAME_LENGTH for element in snapshot.elements)
        assert malformed.handle.disposed
        assert not recovered_button.handle.disposed

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("tag", "role", "input_type", "editable", "expected_clickable"),
    [
        ("button", "", "", False, True),
        ("input", "textbox", "text", True, False),
    ],
)
def test_real_state_uses_supported_element_handle_api(
    tag: str, role: str, input_type: str, editable: bool, expected_clickable: bool
) -> None:
    class NativeHandle:
        def __init__(self) -> None:
            self.evaluations: list[str] = []

        async def evaluate(self, expression: str) -> object:
            self.evaluations.append(expression)
            if expression == "node => node.isConnected":
                return True
            assert expression == "node => node.tagName.toLowerCase()"
            return tag

        async def is_visible(self) -> bool:
            return True

        async def is_enabled(self) -> bool:
            return True

        async def is_editable(self) -> bool:
            if expected_clickable:
                raise RuntimeError("clickable element editability must not be queried")
            return editable

        async def get_attribute(self, name: str) -> str | None:
            return {"type": input_type, "role": role, "href": None}[name]

    async def scenario() -> None:
        handle = NativeHandle()
        session = _RealSession(object(), object(), frozenset(), 1)
        assert await session.state(handle) == (
            True, True, editable, expected_clickable, input_type,
        )
        assert handle.evaluations == [
            "node => node.isConnected", "node => node.tagName.toLowerCase()",
        ]

    asyncio.run(scenario())


def test_real_state_detached_handle_stops_before_later_state_calls() -> None:
    class DetachedHandle:
        async def evaluate(self, expression: str) -> bool:
            assert expression == "node => node.isConnected"
            return False

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"detached state called {name}")

    async def scenario() -> None:
        session = _RealSession(object(), object(), frozenset(), 1)
        assert await session.state(DetachedHandle()) == (False, False, False, False, "")

    asyncio.run(scenario())


def test_playwright_element_handle_has_no_is_connected_method() -> None:
    playwright = pytest.importorskip("playwright.async_api")
    assert not hasattr(playwright.ElementHandle, "is_connected")


@pytest.mark.parametrize(
    ("snapshot", "role", "expected"),
    [
        ('- textbox "Email": hello', "textbox", "Email"),
        ('- textbox "Email" [disabled]: hello', "textbox", "Email"),
        ('- checkbox "Accept" [checked]', "checkbox", "Accept"),
        ('- textbox "Email" [disabled] [readonly]: hello', "textbox", "Email"),
    ],
)
def test_parse_root_aria_name_accepts_real_playwright_grammar(
    snapshot: str, role: str, expected: str
) -> None:
    assert _parse_root_aria_name(snapshot, role) == expected


@pytest.mark.parametrize(
    ("snapshot", "role"),
    [
        ('- button "Email": hello', "textbox"),
        ('- textbox "Email" [disabled', "textbox"),
        ('- textbox "Email": hello\n- button "Injected"', "textbox"),
        ('\n- textbox "Email": hello', "textbox"),
    ],
)
def test_parse_root_aria_name_rejects_wrong_role_and_malformed_or_multiline_input(
    snapshot: str, role: str
) -> None:
    with pytest.raises(ValueError, match="invalid aria snapshot"):
        _parse_root_aria_name(snapshot, role)


def test_real_adapter_creates_isolated_context_and_arms_policy_before_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Page(Emitter):
        url = "about:blank"
        frames = [object()]

        def is_closed(self) -> bool:
            return False

    class Context(Emitter):
        def __init__(self) -> None:
            super().__init__()
            self.order: list[str] = []
            self.websocket_handler: object | None = None
            self.pages: list[Page] = []

        async def route(self, pattern: str, handler: object) -> None:
            assert pattern == "**/*"
            self.order.append("route")

        async def route_web_socket(self, pattern: str, handler: object) -> None:
            assert pattern == "**"
            self.order.append("websocket")
            self.websocket_handler = handler

        async def new_page(self) -> Page:
            self.order.append("page")
            page = Page()
            self.pages.append(page)
            return page

        def is_closed(self) -> bool:
            return False

    class Chromium:
        def __init__(self) -> None:
            self.context = Context()
            self.options: dict[str, object] = {}

        async def launch_persistent_context(
            self, user_data_dir: str, *, headless: bool, **options: object
        ) -> Context:
            assert user_data_dir == str(Path.cwd() / "browser-profile")
            assert headless is False
            self.options = options
            return self.context

    class Playwright:
        chromium = Chromium()

        async def stop(self) -> None:
            return None

    class Starter:
        async def start(self) -> Playwright:
            return Playwright()

    class WebSocket:
        calls = 0

        async def close(self) -> None:
            self.calls += 1

    async_api = types.ModuleType("playwright.async_api")
    async_api.async_playwright = lambda: Starter()  # type: ignore[attr-defined]
    package = types.ModuleType("playwright")
    package.__path__ = []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api)
    browser = Playwright.chromium

    async def scenario() -> None:
        session = await _RealSession.create(
            Path.cwd() / "browser-profile",
            frozenset({"http://127.0.0.1:8899"}),
            False,
            0.1,
            0.1,
        )
        assert browser.options == {"accept_downloads": False, "service_workers": "block"}
        assert browser.context.order == ["route", "websocket", "page"]
        websocket = WebSocket()
        assert browser.context.websocket_handler is not None
        browser.context.websocket_handler(websocket)  # type: ignore[operator]
        assert session.unavailable.is_set()
        await asyncio.sleep(0)
        assert websocket.calls == 1
        session.page = None

    asyncio.run(scenario())

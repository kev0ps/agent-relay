from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

import agent_relay.capabilities.browser as browser_module
from agent_relay.capabilities.browser import (
    BrowserCapability,
    BrowserSnapshot,
    BrowserStartupError,
    BrowserUnavailableError,
    LocalActionError,
    _parse_root_aria_name,
    _RealSession,
)
from agent_relay.output_models import ProviderToolResult
from agent_relay.protocol import (
    MAX_BROWSER_ELEMENTS,
    MAX_BROWSER_PAGE_TEXT_LENGTH,
    InvokeMessage,
)


@dataclass
class Handle:
    visible: bool = True
    enabled: bool = True
    editable: bool = False
    clickable: bool = False
    input_type: str = "text"


class FakeSession:
    def __init__(self) -> None:
        self.url = "http://127.0.0.1:8899/"
        self.title = "fixture"
        self.handles = [
            Handle(editable=True),
            Handle(clickable=True),
            Handle(editable=True, input_type="password"),
        ]
        self.located: list[dict[str, object]] = []
        self.filled: list[tuple[Handle, str]] = []
        self.typed: list[tuple[Handle, str]] = []
        self.clicked: list[Handle] = []
        self.scrolled: list[str] = []
        self.back_calls = 0
        self.reset_calls = 0
        self.closed = 0
        self.navigate_to: str | None = None
        self.unavailable = asyncio.Event()

    async def snapshot(self) -> BrowserSnapshot:
        elements = (
            {
                "locator": {
                    "by": "role",
                    "role": "textbox",
                    "name": "field",
                    "exact": True,
                    "index": 0,
                },
                "role": "textbox",
                "name": "field",
                "value": "",
                "editable": True,
                "enabled": True,
                "clickable": False,
            },
            {
                "locator": {
                    "by": "role",
                    "role": "button",
                    "name": "submit",
                    "exact": True,
                    "index": 0,
                },
                "role": "button",
                "name": "submit",
                "value": None,
                "editable": False,
                "enabled": True,
                "clickable": True,
            },
            {
                "locator": {
                    "by": "role",
                    "role": "textbox",
                    "name": "password",
                    "exact": True,
                    "index": 1,
                },
                "role": "textbox",
                "name": "password",
                "value": "secret",
                "input_type": "password",
                "editable": True,
                "enabled": True,
                "clickable": False,
            },
        )
        return BrowserSnapshot(
            url=self.url,
            title=self.title,
            text="x" * (MAX_BROWSER_PAGE_TEXT_LENGTH + 1000),
            elements=elements * (MAX_BROWSER_ELEMENTS + 2),
        )

    async def locate(self, locator: dict[str, object]) -> Handle:
        self.located.append(dict(locator))
        role = locator.get("role")
        if role == "button":
            return self.handles[1]
        if role == "textbox" and locator.get("name") == "password":
            return self.handles[2]
        return self.handles[0]

    async def navigate(self, url: str) -> None:
        self.navigate_to = url
        self.url = url

    async def fill(self, locator: Handle, value: str) -> None:
        self.filled.append((locator, value))

    async def type(self, locator: Handle, text: str) -> None:
        self.typed.append((locator, text))

    async def click(self, locator: Handle) -> None:
        self.clicked.append(locator)

    async def state(self, locator: Handle) -> tuple[bool, bool, bool, bool, str]:
        return locator.visible, locator.enabled, locator.editable, locator.clickable, locator.input_type

    async def scroll(self, direction: str) -> None:
        self.scrolled.append(direction)

    async def back(self) -> bool:
        self.back_calls += 1
        return True

    async def reset(self) -> None:
        self.reset_calls += 1
        self.url = "http://127.0.0.1:8899/"

    async def wait_unavailable(self) -> None:
        await self.unavailable.wait()

    async def aclose(self) -> None:
        self.closed += 1

    def ensure(self, *, allow_blank: bool = False) -> None:
        if not allow_blank and self.url == "about:blank":
            raise LocalActionError()


def make_capability(
    session: FakeSession,
    *,
    origins: tuple[str, ...] = ("http://127.0.0.1:8899",),
    origin_policy: str = "allowlist",
    action_timeout_seconds: float = 1,
) -> BrowserCapability:
    return BrowserCapability(
        Path.cwd() / "browser-profile",
        origins,
        origin_policy=origin_policy,  # type: ignore[arg-type]
        action_timeout_seconds=action_timeout_seconds,
        adapter_factory=lambda *_: session,  # type: ignore[arg-type]
    )


def structured(result: ProviderToolResult) -> dict[str, object]:
    assert result.structured_content is not None
    return result.structured_content


def test_browser_exposes_generic_provider_descriptors_without_relay_handles() -> None:
    async def scenario() -> None:
        capability = make_capability(FakeSession())
        descriptors = await capability.list_tools()
        assert [descriptor.tool_name for descriptor in descriptors] == [
            "list_tabs",
            "navigate",
            "back",
            "scroll",
            "snapshot",
            "type",
            "fill",
            "click",
        ]
        assert not hasattr(browser_module, "BrowserElement")
        assert not hasattr(browser_module, "BrowserHandle")
        assert not hasattr(browser_module, "_Record")
        assert all("element_id" not in descriptor.input_schema for descriptor in descriptors)
        fill = next(item for item in descriptors if item.tool_name == "fill")
        locator = fill.input_schema["properties"]["locator"]
        assert locator["properties"]["by"]["enum"] == [
            "role",
            "label",
            "placeholder",
            "text",
            "test_id",
        ]
        assert "$ref" not in str(fill.input_schema)

    asyncio.run(scenario())


def test_browser_provider_call_path_resolves_fresh_locators_and_returns_generic_results() -> None:
    async def scenario() -> None:
        session = FakeSession()
        capability = make_capability(session)
        await capability.start()

        tabs = await capability.call_tool("list_tabs", {})
        assert structured(tabs) == {
            "tabs": [{"title": "fixture", "url": "http://127.0.0.1:8899/"}]
        }
        page = structured(await capability.call_tool("snapshot", {}))
        assert len(page["text"]) == MAX_BROWSER_PAGE_TEXT_LENGTH
        assert "secret" not in json.dumps(page)
        assert len(page["elements"]) == MAX_BROWSER_ELEMENTS
        locator = page["elements"][0]["locator"]
        button_locator = page["elements"][1]["locator"]
        assert "element_id" not in str(page)

        fill = structured(
            await capability.call_tool("fill", {"locator": locator, "value": "hello"})
        )
        typed = structured(
            await capability.call_tool("type", {"locator": locator, "text": "typed"})
        )
        clicked = structured(await capability.call_tool("click", {"locator": button_locator}))
        await capability.call_tool("scroll", {"direction": "down"})
        await capability.call_tool("back", {})

        assert fill["success"] is True and "element_id" not in fill
        assert typed["success"] is True and "element_id" not in typed
        assert clicked["success"] is True and "element_id" not in clicked
        assert session.filled == [(session.handles[0], "hello")]
        assert session.typed == [(session.handles[0], "typed")]
        assert session.clicked == [session.handles[1]]
        assert session.scrolled == ["down"]
        assert session.back_calls == 1
        assert len(session.located) == 3

        message = InvokeMessage(
            version=2,
            type="invoke",
            request_id="request",
            tool_name="browser.snapshot",
            arguments={},
        )
        compatibility = await capability.invoke(message)
        assert "structuredContent" not in compatibility
        assert compatibility["elements"]
        await capability.aclose()

    asyncio.run(scenario())


def test_browser_locator_arguments_fail_closed_before_backend_dispatch() -> None:
    async def scenario() -> None:
        session = FakeSession()
        capability = make_capability(session)
        await capability.start()
        invalid = (
            ("click", {}),
            ("click", {"locator": {"by": "css", "value": "button"}}),
            ("click", {"locator": {"by": "role", "name": "submit"}}),
            ("click", {"locator": {"by": "role", "role": "button", "element_id": "x"}}),
            ("fill", {"locator": {"by": "role", "role": "textbox"}, "value": 7}),
            ("scroll", {"direction": "sideways"}),
            ("back", {"unexpected": True}),
        )
        for tool_name, arguments in invalid:
            with pytest.raises(LocalActionError):
                await capability.call_tool(tool_name, arguments)  # type: ignore[arg-type]
        assert session.located == []
        assert session.filled == []
        assert session.clicked == []
        assert session.scrolled == []
        assert session.back_calls == 0

    asyncio.run(scenario())


def test_browser_provider_locator_strategies_and_unknown_tools_are_bounded() -> None:
    async def scenario() -> None:
        capability = make_capability(FakeSession())
        await capability.start()
        for locator in (
            {"by": "label", "value": "Email", "exact": True},
            {"by": "placeholder", "value": "Email", "exact": False},
            {"by": "text", "value": "Submit", "index": 0},
            {"by": "test_id", "value": "submit"},
        ):
            with pytest.raises(LocalActionError):
                await capability.call_tool("click", {"locator": locator})
        with pytest.raises(Exception) as error:
            await capability.call_tool("browser.nope", {})
        assert "unknown" in str(error.value)

    asyncio.run(scenario())


def test_browser_origin_policy_rejects_non_http_and_forbidden_navigation() -> None:
    async def scenario() -> None:
        session = FakeSession()
        capability = make_capability(session)
        await capability.start()
        for url in (
            "file:///tmp/page.html",
            "javascript:alert(1)",
            "data:text/html,blocked",
            "https://example.test/",
        ):
            with pytest.raises(LocalActionError):
                await capability.call_tool("navigate", {"url": url})
        any_session = FakeSession()
        any_capability = make_capability(any_session, origins=(), origin_policy="any")
        await any_capability.start()
        result = structured(
            await any_capability.call_tool("navigate", {"url": "https://example.test/path"})
        )
        assert result["url"] == "https://example.test/path"
        await capability.aclose()
        await any_capability.aclose()

    asyncio.run(scenario())


def test_browser_startup_failure_is_terminal_and_sanitized() -> None:
    async def fail(*_args: object) -> FakeSession:
        raise RuntimeError("profile lock: /private/path")

    async def scenario() -> None:
        capability = BrowserCapability(
            Path.cwd() / "browser-profile",
            ("http://127.0.0.1:8899",),
            adapter_factory=fail,  # type: ignore[arg-type]
        )
        with pytest.raises(BrowserStartupError) as error:
            await capability.start()
        assert str(error.value) == ""
        with pytest.raises(BrowserUnavailableError):
            await capability.call_tool("snapshot", {})

    asyncio.run(scenario())


def test_browser_cancellation_resets_owned_session_and_preserves_cancel() -> None:
    async def scenario() -> None:
        session = FakeSession()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked(url: str) -> None:
            entered.set()
            await release.wait()
            session.url = url

        session.navigate = blocked  # type: ignore[method-assign]
        capability = make_capability(session)
        await capability.start()
        task = asyncio.create_task(
            capability.call_tool("navigate", {"url": "http://127.0.0.1:8899/blocked"})
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.reset_calls == 1
        await capability.aclose()

    asyncio.run(scenario())


def test_browser_unavailability_detaches_and_closes_owned_session() -> None:
    async def scenario() -> None:
        session = FakeSession()
        capability = make_capability(session)
        await capability.start()
        unavailable = asyncio.create_task(capability.wait_unavailable())
        session.unavailable.set()
        await unavailable
        await capability.aclose()
        assert session.closed == 1
        with pytest.raises(BrowserUnavailableError):
            await capability.call_tool("snapshot", {})

    asyncio.run(scenario())


def test_about_blank_can_be_listed_but_not_read_or_used_as_action_target() -> None:
    async def scenario() -> None:
        session = FakeSession()
        session.url = "about:blank"
        session.title = ""
        capability = make_capability(session)
        await capability.start()
        tabs = structured(await capability.call_tool("list_tabs", {}))
        assert tabs == {"tabs": [{"title": "", "url": "about:blank"}]}
        with pytest.raises(LocalActionError):
            await capability.call_tool("snapshot", {})
        with pytest.raises(LocalActionError):
            await capability.call_tool("click", {"locator": {"by": "role", "role": "button"}})
        await capability.aclose()

    asyncio.run(scenario())


def test_snapshot_rejects_relay_owned_handle_fields() -> None:
    class BadSession(FakeSession):
        async def snapshot(self) -> BrowserSnapshot:
            return BrowserSnapshot(
                self.url,
                self.title,
                "content",
                ({"element_id": "relay-owned", "locator": {"by": "role", "role": "button"}},),
            )

    async def scenario() -> None:
        session = BadSession()
        capability = make_capability(session)
        await capability.start()
        with pytest.raises(LocalActionError):
            await capability.call_tool("snapshot", {})
        await capability.aclose()

    asyncio.run(scenario())


def test_real_session_event_callbacks_mark_provider_unavailable() -> None:
    class Emitter:
        def __init__(self) -> None:
            self.handlers: dict[str, list[object]] = {}

        def on(self, event: str, callback: object) -> None:
            self.handlers.setdefault(event, []).append(callback)

        def emit(self, event: str, value: object) -> None:
            for callback in self.handlers[event]:
                callback(value)  # type: ignore[operator]

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


def test_root_aria_parser_accepts_only_expected_single_root() -> None:
    assert _parse_root_aria_name('- button "Submit"', "button") == "Submit"
    assert _parse_root_aria_name("- button 'Submit'", "button") == "Submit"
    with pytest.raises(ValueError):
        _parse_root_aria_name('- link "Submit"', "button")
    with pytest.raises(ValueError):
        _parse_root_aria_name('- button "Submit"\n- text "nested"', "button")

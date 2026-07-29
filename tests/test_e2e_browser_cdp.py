from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def _load_browser_cdp() -> ModuleType:
    path = Path(__file__).parent / "e2e" / "browser_cdp.py"
    spec = importlib.util.spec_from_file_location("_test_browser_cdp", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


browser_cdp = _load_browser_cdp()


def _png(*, width: int = 1280, height: int = 800) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0dIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
    )


def test_fixture_page_socket_requires_one_exact_fixture_target() -> None:
    fixture_url = "http://127.0.0.1:8000/"
    payload = [
        {
            "type": "page",
            "url": "http://127.0.0.1:8000/other",
            "webSocketDebuggerUrl": "ws://127.0.0.1/other",
        },
        {
            "type": "page",
            "url": fixture_url,
            "webSocketDebuggerUrl": "ws://127.0.0.1/fixture",
        },
    ]

    assert (
        browser_cdp.fixture_page_socket(
            "http://127.0.0.1:9222",
            fixture_url,
            fetch_json=lambda url: payload,
        )
        == "ws://127.0.0.1/fixture"
    )


@pytest.mark.parametrize("payload", [[], [{"type": "page"}], [{}, {}] * 5])
def test_fixture_page_socket_rejects_invalid_target_inventory(payload: object) -> None:
    with pytest.raises(browser_cdp.BrowserCDPError):
        browser_cdp.fixture_page_socket(
            "http://127.0.0.1:9222",
            "http://127.0.0.1:8000/",
            fetch_json=lambda url: payload,
        )


def test_capture_png_ignores_uncorrelated_messages_and_validates_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _png()
    responses = [
        json.dumps({"method": "Page.frameNavigated", "params": {}}),
        json.dumps(
            {
                "id": 1,
                "result": {"data": base64.b64encode(image).decode("ascii")},
            }
        ),
    ]
    sent: list[str] = []

    class Socket:
        async def send(self, message: str) -> None:
            sent.append(message)

        async def recv(self) -> str:
            return responses.pop(0)

    class Connection:
        async def __aenter__(self) -> Socket:
            return Socket()

        async def __aexit__(self, *_args: object) -> None:
            return None

    def connect(url: str, **kwargs: object) -> Connection:
        assert url == "ws://127.0.0.1/fixture"
        assert kwargs == {"open_timeout": 2, "close_timeout": 2, "max_size": 1024}
        return Connection()

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))

    result = asyncio.run(
        browser_cdp.capture_png(
            "ws://127.0.0.1/fixture",
            max_cdp_frame_bytes=1024,
            max_screenshot_bytes=512,
            max_screenshot_dimension=4096,
        )
    )

    assert result == image
    assert json.loads(sent[0]) == {
        "id": 1,
        "method": "Page.captureScreenshot",
        "params": {"format": "png"},
    }


def test_capture_fixture_png_includes_target_discovery_in_global_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    times = iter((100.0, 115.0))
    capture_called = False

    async def capture_png(ws_url: str) -> bytes:
        nonlocal capture_called
        capture_called = True
        return _png()

    monkeypatch.setattr(browser_cdp.time, "monotonic", lambda: next(times))

    with pytest.raises(browser_cdp.BrowserCDPError, match="timed out"):
        browser_cdp.capture_fixture_png(
            "http://127.0.0.1:9222",
            "http://127.0.0.1:8000/",
            fixture_page_socket=lambda *_args: "ws://127.0.0.1/fixture",
            capture_png=capture_png,
            timeout_seconds=15.0,
        )

    assert not capture_called


def test_capture_png_rejects_extra_response_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _png()

    class Socket:
        async def send(self, message: str) -> None:
            return None

        async def recv(self) -> str:
            return json.dumps(
                {
                    "id": 1,
                    "result": {"data": base64.b64encode(image).decode("ascii")},
                    "extra": True,
                }
            )

    class Connection:
        async def __aenter__(self) -> Socket:
            return Socket()

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setitem(
        sys.modules,
        "websockets",
        SimpleNamespace(connect=lambda *_args, **_kwargs: Connection()),
    )

    with pytest.raises(browser_cdp.BrowserCDPError, match="extra fields"):
        asyncio.run(
            browser_cdp.capture_png(
                "ws://127.0.0.1/fixture",
                max_cdp_frame_bytes=1024,
                max_screenshot_bytes=512,
                max_screenshot_dimension=4096,
            )
        )


def test_validate_screenshot_png_rejects_zero_or_oversized_dimensions() -> None:
    with pytest.raises(browser_cdp.BrowserCDPError, match="dimensions"):
        browser_cdp.validate_screenshot_png(
            _png(width=0),
            max_screenshot_bytes=512,
            max_screenshot_dimension=4096,
        )
    with pytest.raises(browser_cdp.BrowserCDPError, match="dimensions"):
        browser_cdp.validate_screenshot_png(
            _png(width=4097),
            max_screenshot_bytes=512,
            max_screenshot_dimension=4096,
        )

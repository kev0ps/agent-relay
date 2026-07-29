"""Portable bounded CDP screenshot protocol for native Browser E2E harnesses."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
import urllib.error
from collections.abc import Awaitable, Callable


class BrowserCDPError(RuntimeError):
    """Raised when Chromium returns an invalid or unsafe CDP screenshot payload."""


def fixture_page_socket(
    cdp_url: str,
    fixture_url: str,
    *,
    fetch_json: Callable[[str], object],
) -> str:
    """Return the unique CDP socket for the exact fixture page."""
    try:
        payload = fetch_json(f"{cdp_url}/json/list")
    except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as error:
        raise BrowserCDPError("Chromium page listing failed") from error
    if not isinstance(payload, list) or not 1 <= len(payload) <= 8:
        raise BrowserCDPError("Chromium page listing is invalid")
    matches = [
        item
        for item in payload
        if isinstance(item, dict)
        and item.get("type") == "page"
        and item.get("url") == fixture_url
        and isinstance(item.get("webSocketDebuggerUrl"), str)
    ]
    if len(matches) != 1:
        raise BrowserCDPError("Chromium fixture page identity is invalid")
    return matches[0]["webSocketDebuggerUrl"]


def capture_fixture_png(
    cdp_url: str,
    fixture_url: str,
    *,
    fixture_page_socket: Callable[[str, str], str],
    capture_png: Callable[[str], Awaitable[bytes]],
    timeout_seconds: float,
) -> bytes:
    """Discover and capture the fixture target within one total time budget."""
    if not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 60:
        raise ValueError("CDP screenshot timeout must be between 0 and 60 seconds")
    started_at = time.monotonic()
    ws_url = fixture_page_socket(cdp_url, fixture_url)
    remaining = timeout_seconds - (time.monotonic() - started_at)
    if remaining <= 0:
        raise BrowserCDPError("Chromium screenshot timed out")
    try:
        return asyncio.run(
            asyncio.wait_for(
                capture_png(ws_url),
                timeout=remaining,
            )
        )
    except TimeoutError:
        raise BrowserCDPError("Chromium screenshot timed out") from None


async def capture_png(
    ws_url: str,
    *,
    max_cdp_frame_bytes: int,
    max_screenshot_bytes: int,
    max_screenshot_dimension: int,
) -> bytes:
    """Capture and validate one PNG through the bounded raw CDP protocol."""
    try:
        import websockets
    except ImportError as error:
        raise BrowserCDPError("WebSocket client is unavailable") from error

    async with websockets.connect(
        ws_url,
        open_timeout=2,
        close_timeout=2,
        max_size=max_cdp_frame_bytes,
    ) as socket:
        await socket.send(
            json.dumps(
                {
                    "id": 1,
                    "method": "Page.captureScreenshot",
                    "params": {"format": "png"},
                },
                separators=(",", ":"),
            )
        )
        while True:
            raw = await asyncio.wait_for(socket.recv(), timeout=5)
            if not isinstance(raw, str):
                raise BrowserCDPError("Chromium CDP returned binary framing")
            try:
                message = json.loads(raw)
            except (TypeError, ValueError) as error:
                raise BrowserCDPError("Chromium CDP response is invalid") from error
            if not isinstance(message, dict) or message.get("id") != 1:
                continue
            if set(message) - {"id", "result", "error"}:
                raise BrowserCDPError("Chromium CDP returned extra fields")
            if "error" in message:
                raise BrowserCDPError("Chromium screenshot request failed")
            result = message.get("result")
            if not isinstance(result, dict) or set(result) != {"data"}:
                raise BrowserCDPError("Chromium screenshot result is invalid")
            data = result["data"]
            if not isinstance(data, str) or len(data) > max_screenshot_bytes * 2:
                raise BrowserCDPError("Chromium screenshot is oversized")
            try:
                image = base64.b64decode(data, validate=True)
            except (binascii.Error, ValueError) as error:
                raise BrowserCDPError("Chromium screenshot encoding is invalid") from error
            validate_screenshot_png(
                image,
                max_screenshot_bytes=max_screenshot_bytes,
                max_screenshot_dimension=max_screenshot_dimension,
            )
            return image


def validate_screenshot_png(
    payload: bytes,
    *,
    max_screenshot_bytes: int,
    max_screenshot_dimension: int,
) -> tuple[int, int]:
    """Validate a bounded PNG signature, IHDR, and non-zero dimensions."""
    if not isinstance(payload, bytes) or not 24 <= len(payload) <= max_screenshot_bytes:
        raise BrowserCDPError("Chromium screenshot is not a bounded PNG")
    if payload[:8] != b"\x89PNG\r\n\x1a\n" or payload[12:16] != b"IHDR":
        raise BrowserCDPError("Chromium screenshot is not a bounded PNG")
    width = int.from_bytes(payload[16:20], "big")
    height = int.from_bytes(payload[20:24], "big")
    if (
        not 1 <= width <= max_screenshot_dimension
        or not 1 <= height <= max_screenshot_dimension
    ):
        raise BrowserCDPError("Chromium screenshot dimensions are invalid")
    return width, height

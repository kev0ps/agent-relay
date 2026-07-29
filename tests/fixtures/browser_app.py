#!/usr/bin/env python3
"""Deterministic loopback-only browser fixture for the Linux E2E client."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8899
MAX_BODY = 2048
MAX_VALUE = 256
RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
PAGE = Path(__file__).with_name("browser_page.html").read_bytes()
HEALTH = b'{"status":"ready"}'
INVALID = b'{"error":"invalid request"}'


def _valid_text(value: object, limit: int) -> bool:
    return (
        type(value) is str
        and len(value) <= limit
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def append_event(artifacts: Path, run_id: str, value: str) -> None:
    payload = json.dumps(
        {"run_id": run_id, "event": "submitted", "value": value},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii") + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(artifacts / "browser-events.jsonl", flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("unsafe artifact")
        with os.fdopen(descriptor, "ab", closefd=True) as stream:
            descriptor = -1
            written = stream.write(payload)
            if written != len(payload):
                raise OSError("short artifact write")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def make_handler(artifacts: Path, run_id: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "RelayFixture"
        sys_version = ""

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _reply(self, status_code: int, body: bytes = b"", content_type: str | None = None) -> None:
            self.send_response_only(status_code)
            if content_type is not None:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/":
                self._reply(200, PAGE, "text/html; charset=utf-8")
            elif self.path == "/health":
                self._reply(200, HEALTH, "application/json")
            else:
                self._reply(404, INVALID, "application/json")

        def do_POST(self) -> None:
            self.close_connection = True
            if self.path != "/event":
                self._reply(404, INVALID, "application/json")
                return
            lengths = self.headers.get_all("Content-Length", [])
            transfer_encodings = self.headers.get_all("Transfer-Encoding", [])
            if len(lengths) != 1 or transfer_encodings:
                self.close_connection = True
                self._reply(400, INVALID, "application/json")
                return
            raw_length = lengths[0]
            if (
                not raw_length.isascii()
                or not raw_length.isdecimal()
                or len(raw_length) > len(str(MAX_BODY))
            ):
                self._reply(400, INVALID, "application/json")
                return
            length = int(raw_length)
            if length == 0 or length > MAX_BODY:
                self._reply(400, INVALID, "application/json")
                return
            content_types = self.headers.get_all("Content-Type", [])
            if content_types != ["application/json"]:
                self._reply(415, INVALID, "application/json")
                return
            body = self.rfile.read(length)
            if len(body) != length or self._has_trailing_bytes():
                self._reply(400, INVALID, "application/json")
                return
            try:
                payload = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._reply(400, INVALID, "application/json")
                return
            if (
                type(payload) is not dict
                or set(payload) != {"event", "value"}
                or payload.get("event") != "submitted"
                or not _valid_text(payload.get("value"), MAX_VALUE)
            ):
                self._reply(400, INVALID, "application/json")
                return
            try:
                append_event(artifacts, run_id, payload["value"])
            except OSError:
                self._reply(500)
                return
            self._reply(204)

        def _has_trailing_bytes(self) -> bool:
            try:
                previous_timeout = self.connection.gettimeout()
            except OSError:
                return True
            trailing = True
            try:
                try:
                    self.connection.settimeout(0)
                    try:
                        trailing = bool(self.rfile.peek(1))
                    except BlockingIOError:
                        trailing = False
                    except (OSError, ValueError):
                        pass
                except OSError:
                    pass
            finally:
                try:
                    self.connection.settimeout(previous_timeout)
                except OSError:
                    trailing = True
            return trailing

        def do_PUT(self) -> None:
            self._reply(405, INVALID, "application/json")

        def do_DELETE(self) -> None:
            self._reply(405, INVALID, "application/json")

        def do_PATCH(self) -> None:
            self._reply(405, INVALID, "application/json")

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    arguments = parser.parse_args()
    if RUN_ID.fullmatch(arguments.run_id) is None:
        parser.error("invalid run id")
    if arguments.host not in {"127.0.0.1", "localhost"}:
        parser.error("host must be loopback")
    if type(arguments.port) is not int or not 1 <= arguments.port <= 65535:
        parser.error("port must be valid")
    artifacts = Path(os.environ.get("ARTIFACTS_DIR", "/artifacts"))
    server = ThreadingHTTPServer((arguments.host, arguments.port), make_handler(artifacts, arguments.run_id))
    server.serve_forever()


if __name__ == "__main__":
    main()

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
APP = ROOT / "scripts" / "e2e" / "fixtures" / "cua" / "server.py"
PAGE = ROOT / "scripts" / "e2e" / "fixtures" / "cua" / "index.html"
PORT = 8898


def _load_app(name: str):
    spec = importlib.util.spec_from_file_location(name, APP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _wait_ready() -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            status, _, body = _request("GET", "/health")
            if status == 200 and body == b'{"status":"ready"}':
                return
        except OSError:
            time.sleep(0.02)
    raise AssertionError("desktop fixture did not become ready")


def _choose_port() -> int:
    """Reserve one ephemeral loopback port for an isolated fixture process."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _fixture_environment(artifacts: Path) -> dict[str, str]:
    """Keep the fixture isolated while retaining the Windows runtime basics."""
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "ARTIFACTS_DIR": str(artifacts),
    }
    if os.name == "nt":
        for name in (
            "COMSPEC",
            "HOMEDRIVE",
            "HOMEPATH",
            "LOCALAPPDATA",
            "PROGRAMDATA",
            "SYSTEMDRIVE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "WINDIR",
        ):
            if value := os.environ.get(name):
                environment[name] = value
    return environment


def _stop_fixture(process: subprocess.Popen[bytes]) -> None:
    """Stop the fixture process tree with the native primitive for each OS."""
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


@pytest.fixture
def desktop_server(tmp_path: Path):
    global PORT

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    PORT = _choose_port()
    diagnostic_path = tmp_path / "desktop-fixture.stderr.log"
    try:
        with diagnostic_path.open("wb") as diagnostic:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(APP),
                    "--run-id",
                    "desktop.run-1",
                    "--port",
                    str(PORT),
                ],
                env=_fixture_environment(artifacts),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=diagnostic,
                start_new_session=True,
            )
            try:
                _wait_ready()
                yield artifacts
            except BaseException as error:
                diagnostic.flush()
                details = diagnostic_path.read_text(encoding="utf-8", errors="replace").strip()
                if details:
                    raise AssertionError(f"desktop fixture diagnostic: {details}") from error
                raise
            finally:
                _stop_fixture(process)
    finally:
        PORT = 8898


def _request(method: str, path: str, body: bytes = b"", content_type: str | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=2)
    headers = {"Content-Type": content_type} if content_type else {}
    connection.request(method, path, body, headers)
    response = connection.getresponse()
    result = response.status, response.getheaders(), response.read()
    connection.close()
    return result


def _raw_request(request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", PORT), timeout=2) as connection:
        connection.settimeout(2)
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        response = bytearray()
        while chunk := connection.recv(4096):
            response.extend(chunk)
    return bytes(response)


def _raw_post(body: bytes, extra_headers: bytes = b"") -> bytes:
    return _raw_request(
        b"POST /event HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        + extra_headers
        + b"Content-Length: "
        + str(len(body)).encode("ascii")
        + b"\r\n\r\n"
        + body
    )


def test_page_has_exact_stable_accessible_controls_and_safe_script() -> None:
    page = PAGE.read_text()
    assert "<title>Relay Desktop Fixture</title>" in page
    assert page.count("<h1>Relay Desktop Fixture</h1>") == 1
    assert '<label for="name">Name</label>' in page
    assert page.count("<input ") == 2
    assert 'id="name" name="name" required maxlength="64" autocomplete="off"' in page
    assert page.count("<button ") == 3
    assert '<button type="submit" aria-label="Apply">Apply</button>' in page
    assert 'disabled aria-label="Dormant Override"' in page
    assert '<label for="decoy-secret">Vault Password</label>' in page
    assert 'type="password"' in page
    assert 'aria-label="Grant Camera Permission"' in page
    allowed_form = page.split('<form id="relay-form">', 1)[1].split("</form>", 1)[0]
    assert allowed_form.count("<input ") == 1
    assert allowed_form.count("<button ") == 1
    assert "decoy" not in allowed_form.casefold()
    assert "password" not in allowed_form.casefold()
    assert "permission" not in allowed_form.casefold()
    assert '<section aria-label="Adversarial decoys">' in page
    assert '<output id="status" aria-live="polite">idle</output>' in page
    assert "JSON.stringify({event: 'applied', value})" in page
    assert "fetch('/event'" in page
    assert not any(marker in page for marker in ("http://", "https://", "src=", "href="))
    assert not any(
        marker in page
        for marker in ("window.open", "location", "clipboard", "download", "showOpenFilePicker")
    )


def test_routes_are_exact_and_get_has_no_action(desktop_server: Path) -> None:
    status, headers, body = _request("GET", "/health")
    assert status == 200 and body == b'{"status":"ready"}'
    assert dict(headers) == {
        "Content-Type": "application/json",
        "Content-Length": "18",
        "Cache-Control": "no-store",
    }
    status, _, body = _request("GET", "/")
    assert status == 200 and body == PAGE.read_bytes()
    assert not (desktop_server / "computer-events.jsonl").exists()


def test_valid_event_is_canonical_private_durable_append(desktop_server: Path) -> None:
    body = b'{"event":"applied","value":"Relay \\u2603"}'
    response = _raw_post(body)
    assert response.startswith(b"HTTP/1.0 204 ")
    artifact = desktop_server / "computer-events.jsonl"
    assert artifact.read_bytes() == (
        b'{"run_id":"desktop.run-1","event":"applied","value":"Relay \\u2603"}\n'
    )
    if os.name == "posix":
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o600


def test_repeat_action_remains_two_visible_lines(desktop_server: Path) -> None:
    for value in ("one", "two"):
        status, _, _ = _request(
            "POST",
            "/event",
            json.dumps({"event": "applied", "value": value}).encode(),
            "application/json",
        )
        assert status == 204
    assert (desktop_server / "computer-events.jsonl").read_bytes() == (
        b'{"run_id":"desktop.run-1","event":"applied","value":"one"}\n'
        b'{"run_id":"desktop.run-1","event":"applied","value":"two"}\n'
    )


@pytest.mark.parametrize(
    ("method", "path", "body", "content_type"),
    [
        ("POST", "/wrong", b'{"event":"applied","value":"x"}', "application/json"),
        ("PUT", "/event", b'{"event":"applied","value":"x"}', "application/json"),
        ("DELETE", "/event", b"", None),
        ("PATCH", "/event", b"", None),
        ("OPTIONS", "/event", b"", None),
        ("POST", "/event", b"{}", "text/plain"),
        ("POST", "/event", b"{", "application/json"),
        ("POST", "/event", b"[]", "application/json"),
        ("POST", "/event", b'{"event":"applied"}', "application/json"),
        ("POST", "/event", b'{"event":"wrong","value":"x"}', "application/json"),
        ("POST", "/event", b'{"event":"applied","value":1}', "application/json"),
        ("POST", "/event", b'{"event":"applied","value":"x","extra":1}', "application/json"),
        ("POST", "/event", json.dumps({"event": "applied", "value": "x" * 257}).encode(), "application/json"),
        ("POST", "/event", b'{"event":"applied","value":"x\\u0000"}', "application/json"),
        ("POST", "/event", b'{"event":"applied","value":"x\\u0085"}', "application/json"),
        ("POST", "/event", b"x" * 2049, "application/json"),
    ],
)
def test_invalid_requests_do_not_create_artifact(
    desktop_server: Path, method: str, path: str, body: bytes, content_type: str | None
) -> None:
    status, _, response = _request(method, path, body, content_type)
    assert 400 <= status < 500
    assert response in {b"", b'{"error":"invalid request"}'}
    assert not (desktop_server / "computer-events.jsonl").exists()


@pytest.mark.parametrize(
    "body",
    [
        b'{"event":"applied","event":"applied","value":"x"}',
        b'{"event":"applied","value":"x","value":"x"}',
    ],
)
def test_duplicate_json_keys_are_rejected(desktop_server: Path, body: bytes) -> None:
    assert _raw_post(body).startswith(b"HTTP/1.0 400 ")
    assert not (desktop_server / "computer-events.jsonl").exists()


@pytest.mark.parametrize(
    "framing",
    [
        b"Content-Length: {length}\r\nContent-Length: {length}\r\n",
        b"Content-Length: {length}, {length}\r\n",
        b"Transfer-Encoding: chunked\r\n",
        b"Transfer-Encoding: identity\r\nContent-Length: {length}\r\n",
        b"Content-Length: +{length}\r\n",
        b"Content-Length: 0\r\n",
        b"Content-Length: 2049\r\n",
    ],
)
def test_bad_framing_is_rejected_and_connection_closed(
    desktop_server: Path, framing: bytes
) -> None:
    body = b'{"event":"applied","value":"x"}'
    framing = framing.replace(b"{length}", str(len(body)).encode())
    response = _raw_request(
        b"POST /event HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        + framing
        + b"Connection: keep-alive\r\n\r\n"
        + body
        + b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
    )
    assert response.startswith(b"HTTP/1.0 4")
    assert response.count(b"HTTP/") == 1
    assert not (desktop_server / "computer-events.jsonl").exists()


@pytest.mark.parametrize(
    "content_types",
    [
        b"Content-Type: application/json\r\nContent-Type: application/json\r\n",
        b"Content-Type: application/json, application/json\r\n",
    ],
)
def test_ambiguous_content_type_is_rejected(
    desktop_server: Path, content_types: bytes
) -> None:
    body = b'{"event":"applied","value":"x"}'
    response = _raw_request(
        b"POST /event HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        + content_types
        + b"Content-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )
    assert response.startswith(b"HTTP/1.0 4")
    assert not (desktop_server / "computer-events.jsonl").exists()


def test_short_and_trailing_bodies_are_rejected(desktop_server: Path) -> None:
    body = b'{"event":"applied","value":"x"}'
    short = _raw_request(
        b"POST /event HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: "
        + str(len(body) + 1).encode()
        + b"\r\n\r\n"
        + body
    )
    assert short.startswith(b"HTTP/1.0 400 ")
    trailing = _raw_request(
        b"POST /event HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
        + b"x"
    )
    assert trailing.startswith(b"HTTP/1.0 400 ")
    assert not (desktop_server / "computer-events.jsonl").exists()


@pytest.mark.skipif(
    os.name != "posix", reason="requires POSIX no-follow and FIFO primitives"
)
@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_unsafe_artifact_targets_are_refused(tmp_path: Path, kind: str) -> None:
    app = _load_app(f"desktop_fixture_{kind}")
    artifact = tmp_path / "computer-events.jsonl"
    target = tmp_path / "target"
    target.write_text("safe")
    if kind == "symlink":
        artifact.symlink_to(target)
    else:
        os.mkfifo(artifact)
    with pytest.raises(OSError):
        app.append_event(tmp_path, "run-1", "value")
    assert target.read_text() == "safe"


@pytest.mark.parametrize("run_id", ["", "x" * 65, "bad space", "bad/slash", "snowman-☃"])
def test_run_id_is_strictly_validated(run_id: str, tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(APP), "--run-id", run_id],
        env=_fixture_environment(tmp_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=2,
        check=False,
    )
    assert result.returncode == 2
    assert not (tmp_path / "computer-events.jsonl").exists()


def test_server_configuration_is_loopback_with_desktop_default_port() -> None:
    app = _load_app("desktop_fixture_configuration")
    assert app.HOST == "127.0.0.1"
    assert app.PORT == 8898

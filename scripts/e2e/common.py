"""Small cross-platform primitives shared by native E2E adapters."""

from __future__ import annotations

import os
import secrets
import socket
import stat
import sys
from pathlib import Path

MAX_TOKEN_LENGTH = 128
MAX_ARTIFACT_BYTES = 4096
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class E2EError(RuntimeError):
    """Bounded, non-sensitive shared harness failure."""


def generate_credentials() -> tuple[str, str]:
    """Generate distinct, bounded credentials kept only for one E2E run."""
    agent_token = secrets.token_urlsafe(48)
    control_token = secrets.token_urlsafe(48)
    if (
        not agent_token
        or not control_token
        or agent_token == control_token
        or len(agent_token) > MAX_TOKEN_LENGTH
        or len(control_token) > MAX_TOKEN_LENGTH
    ):
        raise E2EError("ephemeral credential generation failed")
    return agent_token, control_token


def choose_loopback_port() -> int:
    """Ask the OS for an unused loopback port for one native run."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _validate_port(port: int) -> None:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be a valid TCP port")


def server_command(port: int) -> list[str]:
    """Return the source-mode server command driven entirely by environment."""
    _validate_port(port)
    return [
        sys.executable,
        "-m",
        "agent_relay.server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]


def agent_command(port: int, workspace: Path) -> list[str]:
    """Return the fixed Relay Agent command; configuration stays in the env."""
    _validate_port(port)
    if not isinstance(workspace, Path) or not workspace.is_absolute():
        raise ValueError("workspace must be an absolute path")
    installed_command = os.environ.get("RELAY_E2E_AGENT_RELAY_COMMAND")
    if installed_command:
        return [installed_command, "agent"]
    return [sys.executable, "-m", "agent_relay.agent"]


def _unsafe_path(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    )


def prepare_artifact_directory(evidence_dir: Path) -> None:
    """Create an evidence directory without following reparse points."""
    if not isinstance(evidence_dir, Path) or _unsafe_path(evidence_dir):
        raise E2EError("unsafe evidence directory")
    evidence_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    current = evidence_dir
    while True:
        if _unsafe_path(current):
            raise E2EError("unsafe evidence directory")
        if current.parent == current:
            return
        current = current.parent


def write_artifact(evidence_dir: Path, name: str, payload: bytes) -> None:
    """Create one bounded evidence file without following existing links."""
    if name not in {"output.log", "success.json"}:
        raise E2EError("unsupported evidence file")
    if not isinstance(payload, bytes) or len(payload) > MAX_ARTIFACT_BYTES:
        raise E2EError("oversized evidence payload")
    prepare_artifact_directory(evidence_dir)
    target = evidence_dir / name
    if _unsafe_path(target):
        raise E2EError("unsafe evidence file")
    try:
        with target.open("xb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise E2EError("unsafe evidence file")
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            target.chmod(0o600)
        except OSError:
            if os.name != "nt":
                raise
    except FileExistsError:
        raise E2EError("evidence file already exists") from None


def write_success(evidence_dir: Path) -> None:
    write_artifact(evidence_dir, "success.json", b'{"status":"passed"}\n')

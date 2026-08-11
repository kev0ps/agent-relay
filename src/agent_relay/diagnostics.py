"""Small, sanitized operator log helpers used by Agent Relay runtime code."""

from __future__ import annotations

import os
import sys
from typing import Literal

LogLevel = Literal["INFO", "WARNING", "DEBUG"]


def emit(level: LogLevel, message: str) -> None:
    """Write one already-sanitized runtime message to stderr."""
    print(f"[{level}] {message}", file=sys.stderr, flush=True)


def info(message: str) -> None:
    emit("INFO", message)


def warning(message: str) -> None:
    emit("WARNING", message)


def debug(message: str, *, enabled: bool | None = None) -> None:
    active = os.environ.get("RELAY_NATIVE_DEBUG") == "1" if enabled is None else enabled
    if active:
        emit("DEBUG", message)

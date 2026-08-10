"""Cross-platform validation for bounded E2E evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import NamedTuple, Sequence

SUCCESS_MARKER = b'{"status":"passed"}'
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


class EvidenceValidationError(ValueError):
    """A bounded evidence directory does not match its declared profile."""


class EvidenceProfile(NamedTuple):
    success_line: str
    output_pattern: re.Pattern[str]
    maximum_file_bytes: int
    event_name: str | None = None
    event_kind: str | None = None
    event_run_pattern: re.Pattern[str] | None = None
    event_value_pattern: re.Pattern[str] | None = None


def _output_pattern(
    *, success: str, cleanup: str, failure_prefix: str, diagnostic_phase: bool = True
) -> re.Pattern[str]:
    diagnostic = r"(?: \(phase-[a-z0-9-]+\))?" if diagnostic_phase else ""
    return re.compile(
        rf"(?:{re.escape(success)}|{re.escape(cleanup)}|"
        rf"{re.escape(failure_prefix)}[a-z0-9-]+: "
        rf"[A-Za-z0-9 _().-]+{diagnostic}\.)"
    )


PROFILES: dict[str, EvidenceProfile] = {
    "linux-terminal": EvidenceProfile(
        success_line="Linux MCP end-to-end scenario passed.",
        output_pattern=_output_pattern(
            success="Linux MCP end-to-end scenario passed.",
            cleanup="Linux E2E cleanup failed.",
            failure_prefix="Linux E2E failed at scenario-",
        ),
        maximum_file_bytes=4096,
    ),
    "linux-browser": EvidenceProfile(
        success_line="Linux Browser smoke scenario passed.",
        output_pattern=_output_pattern(
            success="Linux Browser smoke scenario passed.",
            cleanup="Linux Browser E2E cleanup failed.",
            failure_prefix="Linux Browser E2E failed at scenario-",
        ),
        maximum_file_bytes=524288,
        event_name="browser-events.jsonl",
        event_kind="submitted",
        event_run_pattern=re.compile(r"linux-browser-[0-9a-f]{24}"),
        event_value_pattern=re.compile(
            r"relay-gh-browser-linux-browser-[0-9a-f]{24}"
        ),
    ),
    "linux-cua": EvidenceProfile(
        success_line="Linux CUA smoke scenario passed.",
        output_pattern=_output_pattern(
            success="Linux CUA smoke scenario passed.",
            cleanup="Linux CUA E2E cleanup failed.",
            failure_prefix="Linux CUA E2E failed at scenario-",
        ),
        maximum_file_bytes=4096,
        event_name="computer-events.jsonl",
        event_kind="applied",
        event_run_pattern=re.compile(r"linux-cua-[0-9a-f]{24}"),
        event_value_pattern=re.compile(r"relay-gh-cua-linux-cua-[0-9a-f]{24}"),
    ),
    "windows-terminal": EvidenceProfile(
        success_line="Windows MCP end-to-end scenario passed.",
        output_pattern=_output_pattern(
            success="Windows MCP end-to-end scenario passed.",
            cleanup="Windows E2E cleanup failed.",
            failure_prefix="Windows E2E failed at scenario-",
        ),
        maximum_file_bytes=4096,
    ),
    "windows-browser": EvidenceProfile(
        success_line="Windows Browser smoke scenario passed.",
        output_pattern=_output_pattern(
            success="Windows Browser smoke scenario passed.",
            cleanup="Windows Browser E2E cleanup failed.",
            failure_prefix="Windows Browser E2E failed at scenario-",
        ),
        maximum_file_bytes=524288,
        event_name="browser-events.jsonl",
        event_kind="submitted",
        event_run_pattern=re.compile(r"windows-browser-[0-9a-f]{24}"),
        event_value_pattern=re.compile(
            r"relay-gh-browser-windows-browser-[0-9a-f]{24}"
        ),
    ),
    "windows-cua": EvidenceProfile(
        success_line="Windows CUA smoke scenario passed.",
        output_pattern=_output_pattern(
            success="Windows CUA smoke scenario passed.",
            cleanup="Windows CUA E2E cleanup failed.",
            failure_prefix="Windows CUA E2E failed at scenario-",
            diagnostic_phase=False,
        ),
        maximum_file_bytes=4096,
        event_name="computer-events.jsonl",
        event_kind="applied",
        event_run_pattern=re.compile(r"windows-cua-[0-9a-f]{24}"),
        event_value_pattern=re.compile(r"relay-gh-cua-windows-cua-[0-9a-f]{24}"),
    ),
}


def _is_reparse_point(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT)


def _lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as error:
        raise EvidenceValidationError("required evidence path is unavailable") from error


def _validate_directory(path: Path) -> None:
    metadata = _lstat(path)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(metadata)
    ):
        raise EvidenceValidationError("evidence directory is unsafe")


def _read_regular_file(path: Path, maximum_bytes: int) -> bytes:
    metadata = _lstat(path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(metadata)
    ):
        raise EvidenceValidationError("evidence entry is not a regular file")
    if metadata.st_size > maximum_bytes:
        raise EvidenceValidationError("evidence entry is oversized")
    try:
        with path.open("rb") as stream:
            payload = stream.read(maximum_bytes + 1)
    except OSError as error:
        raise EvidenceValidationError("evidence entry is unreadable") from error
    if len(payload) > maximum_bytes:
        raise EvidenceValidationError("evidence entry is oversized")
    return payload


def _exists(path: Path) -> bool:
    return os.path.lexists(path)


def _validate_event(profile: EvidenceProfile, path: Path) -> None:
    if (
        profile.event_kind is None
        or profile.event_run_pattern is None
        or profile.event_value_pattern is None
    ):
        raise EvidenceValidationError("event profile is incomplete")
    payload = _read_regular_file(path, profile.maximum_file_bytes)
    try:
        lines = payload.decode("utf-8").splitlines()
        event = json.loads(lines[0]) if len(lines) == 1 else None
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceValidationError("event evidence is malformed") from error
    if type(event) is not dict or set(event) != {"event", "run_id", "value"}:
        raise EvidenceValidationError("event evidence schema is invalid")
    if (
        event["event"] != profile.event_kind
        or type(event["run_id"]) is not str
        or profile.event_run_pattern.fullmatch(event["run_id"]) is None
        or type(event["value"]) is not str
        or profile.event_value_pattern.fullmatch(event["value"]) is None
    ):
        raise EvidenceValidationError("event evidence value is invalid")


def validate_evidence(profile_name: str, evidence_dir: Path) -> None:
    """Validate one native gate's allowlisted, bounded evidence directory."""
    try:
        profile = PROFILES[profile_name]
    except KeyError as error:
        raise EvidenceValidationError("unknown evidence profile") from error

    _validate_directory(evidence_dir)
    allowed = {"output.log", "success.json"}
    if profile.event_name is not None:
        allowed.add(profile.event_name)
    try:
        entries = list(evidence_dir.iterdir())
    except OSError as error:
        raise EvidenceValidationError("evidence directory is unreadable") from error
    if any(entry.name not in allowed for entry in entries):
        raise EvidenceValidationError("evidence directory contains an unexpected entry")
    for entry in entries:
        _read_regular_file(entry, profile.maximum_file_bytes)

    output_path = evidence_dir / "output.log"
    output_payload = _read_regular_file(output_path, profile.maximum_file_bytes)
    try:
        output_text = output_payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise EvidenceValidationError("output evidence is not UTF-8") from error
    output_lines = output_text.splitlines()
    if not 1 <= len(output_lines) <= 2 or any(
        profile.output_pattern.fullmatch(line) is None for line in output_lines
    ):
        raise EvidenceValidationError("output evidence is invalid")

    successful = output_text.rstrip("\r\n") == profile.success_line
    success_path = evidence_dir / "success.json"
    if successful:
        if not _exists(success_path):
            raise EvidenceValidationError("success marker is missing")
        marker = _read_regular_file(success_path, profile.maximum_file_bytes)
        if marker.rstrip(b"\r\n") != SUCCESS_MARKER:
            raise EvidenceValidationError("success marker is invalid")
    elif _exists(success_path):
        raise EvidenceValidationError("success marker exists after failure")

    if profile.event_name is not None:
        event_path = evidence_dir / profile.event_name
        if successful and not _exists(event_path):
            raise EvidenceValidationError("success event is missing")
        if _exists(event_path):
            _validate_event(profile, event_path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=sorted(PROFILES))
    parser.add_argument("--evidence-dir", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        validate_evidence(arguments.profile, arguments.evidence_dir)
    except EvidenceValidationError as error:
        print(f"Evidence validation failed: {error}", file=sys.stderr)
        return 1
    print(f"Validated bounded {arguments.profile} evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

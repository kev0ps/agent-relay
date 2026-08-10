from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "validate_e2e_evidence.py"

VALID_PROFILES = (
    (
        "linux-terminal",
        "Linux MCP end-to-end scenario passed.\n",
        None,
        None,
    ),
    (
        "linux-browser",
        "Linux Browser smoke scenario passed.\n",
        "browser-events.jsonl",
        {
            "event": "submitted",
            "run_id": "linux-browser-0123456789abcdef01234567",
            "value": "relay-gh-browser-linux-browser-0123456789abcdef01234567",
        },
    ),
    (
        "linux-cua",
        "Linux CUA smoke scenario passed.\n",
        "computer-events.jsonl",
        {
            "event": "applied",
            "run_id": "linux-cua-0123456789abcdef01234567",
            "value": "relay-gh-cua-linux-cua-0123456789abcdef01234567",
        },
    ),
    (
        "windows-terminal",
        "Windows MCP end-to-end scenario passed.\n",
        None,
        None,
    ),
    (
        "windows-browser",
        "Windows Browser smoke scenario passed.\n",
        "browser-events.jsonl",
        {
            "event": "submitted",
            "run_id": "windows-browser-0123456789abcdef01234567",
            "value": "relay-gh-browser-windows-browser-0123456789abcdef01234567",
        },
    ),
    (
        "windows-cua",
        "Windows CUA smoke scenario passed.\n",
        "computer-events.jsonl",
        {
            "event": "applied",
            "run_id": "windows-cua-0123456789abcdef01234567",
            "value": "relay-gh-cua-windows-cua-0123456789abcdef01234567",
        },
    ),
)


def _load_validator():
    spec = importlib.util.spec_from_file_location("validate_e2e_evidence", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("profile", "success", "event_name", "event"), VALID_PROFILES)
def test_validator_accepts_each_success_profile(
    tmp_path: Path,
    profile: str,
    success: str,
    event_name: str | None,
    event: dict[str, str] | None,
) -> None:
    validator = _load_validator()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "output.log").write_text(success, encoding="utf-8")
    (evidence / "success.json").write_text('{"status":"passed"}\n', encoding="utf-8")
    if event_name is not None and event is not None:
        import json

        (evidence / event_name).write_text(json.dumps(event) + "\n", encoding="utf-8")

    validator.validate_evidence(profile, evidence)


@pytest.mark.parametrize(
    ("profile", "failure"),
    (
        ("linux-terminal", "Linux E2E failed at scenario-start: bounded failure.\n"),
        ("linux-browser", "Linux Browser E2E failed at scenario-start: bounded failure.\n"),
        ("linux-cua", "Linux CUA E2E failed at scenario-start: bounded failure.\n"),
        ("windows-terminal", "Windows E2E failed at scenario-start: bounded failure.\n"),
        ("windows-browser", "Windows Browser E2E failed at scenario-start: bounded failure.\n"),
        ("windows-cua", "Windows CUA E2E failed at scenario-start: bounded failure.\n"),
    ),
)
def test_validator_accepts_bounded_failure_without_success_payloads(
    tmp_path: Path, profile: str, failure: str
) -> None:
    validator = _load_validator()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "output.log").write_text(failure, encoding="utf-8")

    validator.validate_evidence(profile, evidence)


@pytest.mark.parametrize(
    ("filename", "payload"),
    (
        ("unexpected.log", "bounded\n"),
        ("success.json", '{"status":"passed"}\n'),
    ),
)
def test_validator_rejects_unexpected_or_false_success_evidence(
    tmp_path: Path, filename: str, payload: str
) -> None:
    validator = _load_validator()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "output.log").write_text(
        "Linux E2E failed at scenario-start: bounded failure.\n",
        encoding="utf-8",
    )
    (evidence / filename).write_text(payload, encoding="utf-8")

    with pytest.raises(validator.EvidenceValidationError):
        validator.validate_evidence("linux-terminal", evidence)


def test_validator_rejects_oversized_or_symlinked_evidence(tmp_path: Path) -> None:
    validator = _load_validator()
    oversized = tmp_path / "oversized"
    oversized.mkdir()
    (oversized / "output.log").write_bytes(b"x" * 4097)
    with pytest.raises(validator.EvidenceValidationError):
        validator.validate_evidence("linux-terminal", oversized)

    evidence = tmp_path / "symlinked"
    evidence.mkdir()
    target = tmp_path / "outside.log"
    target.write_text("Linux E2E cleanup failed.\n", encoding="utf-8")
    try:
        (evidence / "output.log").symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks unavailable on this host")
    with pytest.raises(validator.EvidenceValidationError):
        validator.validate_evidence("linux-terminal", evidence)


def test_validator_rejects_malformed_optional_event_on_failure(tmp_path: Path) -> None:
    validator = _load_validator()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "output.log").write_text(
        "Linux Browser E2E cleanup failed.\n", encoding="utf-8"
    )
    (evidence / "browser-events.jsonl").write_text(
        '{"event":"submitted","run_id":"wrong","value":"wrong","extra":true}\n',
        encoding="utf-8",
    )

    with pytest.raises(validator.EvidenceValidationError):
        validator.validate_evidence("linux-browser", evidence)


def test_validator_requires_success_marker_and_correlated_event(tmp_path: Path) -> None:
    validator = _load_validator()

    terminal = tmp_path / "terminal"
    terminal.mkdir()
    (terminal / "output.log").write_text(
        "Linux MCP end-to-end scenario passed.\n", encoding="utf-8"
    )
    with pytest.raises(validator.EvidenceValidationError):
        validator.validate_evidence("linux-terminal", terminal)

    browser = tmp_path / "browser"
    browser.mkdir()
    (browser / "output.log").write_text(
        "Windows Browser smoke scenario passed.\n", encoding="utf-8"
    )
    (browser / "success.json").write_text(
        '{"status":"passed"}\n', encoding="utf-8"
    )
    with pytest.raises(validator.EvidenceValidationError):
        validator.validate_evidence("windows-browser", browser)


def test_validator_rejects_unbounded_output_text(tmp_path: Path) -> None:
    validator = _load_validator()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "output.log").write_text(
        "Linux E2E failed with secret diagnostic content.\n",
        encoding="utf-8",
    )

    with pytest.raises(validator.EvidenceValidationError):
        validator.validate_evidence("linux-terminal", evidence)

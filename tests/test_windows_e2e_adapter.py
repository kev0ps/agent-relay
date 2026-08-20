from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path

import pytest

from scripts.e2e.platform.windows import (
    MAX_DIAGNOSTIC_BYTES,
    WindowsE2EError,
    WindowsJob,
    WindowsProcessManager,
    _BasicAccountingInformation,
    _diagnostic_category,
    _drain_diagnostic,
    _LargeInteger,
)


def test_windows_minimal_environment_excludes_unrelated_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross-boundary")
    monkeypatch.setenv("HOME", "untrusted-home")

    environment = WindowsProcessManager().minimal_environment(
        tmp_path,
        {"RELAY_AGENT_ID": "windows-e2e"},
    )

    assert environment["RELAY_AGENT_ID"] == "windows-e2e"
    assert environment["USERPROFILE"] == str(tmp_path)
    assert environment["TEMP"] == str(tmp_path)
    assert environment["TMP"] == str(tmp_path)
    assert environment["HOME"] == str(tmp_path)
    assert "OPENAI_API_KEY" not in environment


def test_windows_minimal_environment_uses_repository_src_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("RELAY_E2E_AGENT_RELAY_COMMAND", raising=False)

    environment = WindowsProcessManager().minimal_environment(tmp_path, {})

    assert environment["PYTHONPATH"] == str(Path(__file__).parents[1] / "src")


@pytest.mark.skipif(os.name == "nt", reason="tests the non-Windows guard")
def test_windows_job_requires_windows() -> None:
    with pytest.raises(WindowsE2EError, match="requires Windows"):
        WindowsJob()


def test_windows_job_accounting_structure_includes_all_kernel_fields() -> None:
    fields = dict(_BasicAccountingInformation._fields_)
    assert fields["ThisPeriodTotalKernelTime"] is _LargeInteger


def test_job_termination_closes_handle_when_accounting_fails() -> None:
    job = object.__new__(WindowsJob)
    job._handle = object()

    class Kernel:
        def __init__(self) -> None:
            self.closed = False

        def TerminateJobObject(self, _handle: object, _exit_code: int) -> bool:
            return True

        def CloseHandle(self, _handle: object) -> bool:
            self.closed = True
            return True

    kernel = Kernel()
    job._kernel32 = kernel

    def fail_accounting() -> int:
        raise WindowsE2EError("accounting unavailable")

    job.active_processes = fail_accounting  # type: ignore[method-assign]
    with pytest.raises(WindowsE2EError, match="accounting unavailable"):
        job.terminate(timeout=0.1, processes=[])

    assert kernel.closed is True
    assert job._handle is None


def test_diagnostic_drain_is_bounded(tmp_path: Path) -> None:
    diagnostic = tmp_path / "child.stderr.log"

    _drain_diagnostic(
        BytesIO(b"prefix\n" + b"x" * (MAX_DIAGNOSTIC_BYTES * 2)),
        diagnostic,
    )

    assert diagnostic.stat().st_size == MAX_DIAGNOSTIC_BYTES
    assert diagnostic.read_bytes().startswith(b"prefix\n")


def test_diagnostics_use_closed_categories(tmp_path: Path) -> None:
    diagnostic = tmp_path / "child.stderr.log"
    diagnostic.write_text(
        "Traceback (most recent call last):\n"
        "  File 'secret-path', line 1\n"
        "SecretCredentialError: token-value\n",
        encoding="utf-8",
    )

    category = _diagnostic_category(diagnostic)

    assert category == "child traceback"
    assert "SecretCredentialError" not in category
    assert "token-value" not in category

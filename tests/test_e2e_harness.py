from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "e2e_harness.py"
ENTRYPOINTS = (
    ROOT / "scripts" / "linux_e2e.py",
    ROOT / "scripts" / "windows_e2e.py",
    ROOT / "scripts" / "linux_computer_e2e.py",
    ROOT / "scripts" / "windows_computer_e2e.py",
)


def _load_harness():
    spec = importlib.util.spec_from_file_location("e2e_harness_contract", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_common_harness_owns_terminal_lifecycle_and_three_core_runs() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "class Lifecycle" in source
    assert "def run_terminal_scenario" in source
    assert source.count("portable_scenarios.run_core_scenario(") == 3
    assert all("class NativeLifecycle" not in path.read_text(encoding="utf-8") for path in ENTRYPOINTS)
    assert all("class WindowsLifecycle" not in path.read_text(encoding="utf-8") for path in ENTRYPOINTS)


def test_common_lifecycle_preserves_primary_failure() -> None:
    harness = _load_harness()
    lifecycle = harness.Lifecycle(RuntimeError, "shared cleanup failed")
    lifecycle.add_cleanup(lambda: (_ for _ in ()).throw(RuntimeError("cleanup detail")))

    with pytest.raises(ValueError, match="primary failure"):
        with lifecycle:
            raise ValueError("primary failure")

    assert str(lifecycle.cleanup_error) == "shared cleanup failed"
    assert str(lifecycle.cleanup_error.__cause__) == "cleanup detail"


def test_common_cua_lifecycle_orders_platform_hooks_and_publishes_evidence(
    tmp_path: Path,
) -> None:
    harness = _load_harness()
    events: list[str] = []

    class FakeProcess:
        pid = 123

        def poll(self) -> None:
            return None

    def mark(name: str):
        def callback(*_args, **_kwargs):
            events.append(name)

        return callback

    def create_context(
        root: Path,
        _evidence_dir: Path | None,
        _agent_token: str,
        _control_token: str,
        run_id: str,
        value: str,
        lifecycle,
    ):
        lifecycle.add_cleanup(mark("cleanup"))
        return harness.CuaContext(
            lifecycle=lifecycle,
            root=root,
            home=root / "home",
            workspace=root / "workspace",
            artifacts=root / "artifacts",
            repository=root,
            mcp_url="http://127.0.0.1:1/mcp",
            runtime=SimpleNamespace(),
            value=value,
            run_id=run_id,
        )

    def start(name: str):
        def callback(_context):
            events.append(name)
            return FakeProcess()

        return callback

    def write_artifact(directory: Path, name: str, payload: bytes) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(payload)

    adapter = harness.CuaAdapter(
        label="fake CUA",
        run_id_prefix="fake-cua-",
        temp_prefix="fake-cua-",
        success_message="fake CUA passed.",
        failure_prefix="fake CUA failed at scenario-",
        cleanup_message="fake CUA cleanup failed",
        error_type=harness.E2EHarnessError,
        lifecycle_factory=lambda: harness.Lifecycle(
            harness.E2EHarnessError, "fake CUA cleanup failed"
        ),
        write_artifact=write_artifact,
        validate_host=mark("validate-host"),
        create_context=create_context,
        prepare_platform=mark("platform"),
        start_server=start("server-start"),
        wait_server=mark("server-ready"),
        start_fixture=start("fixture-start"),
        wait_fixture=mark("fixture-ready"),
        start_agent=start("agent-start"),
        wait_agent=mark("agent-ready"),
        prepare_scenario=mark("scenario-prepare"),
        run_scenario=mark("scenario-run"),
        assert_processes=mark("process-validation"),
        report_failure=mark("report-failure"),
        report_after_cleanup=mark("report-after-cleanup"),
    )

    evidence = tmp_path / "evidence"
    output = evidence / "output.log"
    harness.run_cua_scenario(adapter, evidence, output_file=output)

    assert events == [
        "validate-host",
        "platform",
        "server-start",
        "server-ready",
        "fixture-start",
        "fixture-ready",
        "agent-start",
        "agent-ready",
        "scenario-prepare",
        "scenario-run",
        "process-validation",
        "cleanup",
        "report-after-cleanup",
    ]
    assert output.read_text(encoding="ascii") == "fake CUA passed.\n"
    assert (evidence / "success.json").read_bytes() == harness.SUCCESS_MARKER

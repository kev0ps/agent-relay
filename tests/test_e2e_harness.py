from __future__ import annotations

import builtins
import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.e2e import terminal
from scripts.e2e.common import (
    E2EError,
    agent_command,
    generate_credentials,
    prepare_artifact_directory,
    server_command,
    write_artifact,
)
from scripts.e2e.harness import run_lifecycle
from scripts.e2e.terminal import run_terminal_e2e


@dataclass
class FakeService:
    name: str
    events: list[str]
    failures: dict[str, BaseException]

    def _record(self, action: str) -> None:
        event = f"{self.name}:{action}"
        self.events.append(event)
        if error := self.failures.get(event):
            raise error

    def start(self) -> None:
        self._record("start")

    def wait_ready(self) -> None:
        self._record("ready")

    def stop(self) -> None:
        self._record("stop")

    def wait_stopped(self) -> None:
        self._record("stopped")


@dataclass
class FakeAdapter:
    failures: dict[str, BaseException] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.server = FakeService("server", self.events, self.failures)
        self.agent = FakeService("agent", self.events, self.failures)

    def _record(self, event: str) -> None:
        self.events.append(event)
        if error := self.failures.get(event):
            raise error

    def prepare(self) -> None:
        self._record("prepare")

    def collect_evidence(self) -> None:
        self._record("evidence")

    def cleanup(self) -> None:
        self._record("cleanup")


def scenario(adapter: FakeAdapter) -> None:
    adapter._record("scenario")


def test_shared_credentials_are_distinct_and_bounded() -> None:
    agent_token, control_token = generate_credentials()

    assert agent_token and control_token
    assert agent_token != control_token
    assert len(agent_token) <= 128
    assert len(control_token) <= 128


def test_shared_commands_use_fixed_entrypoints(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("RELAY_E2E_AGENT_RELAY_COMMAND", raising=False)

    assert server_command(23456) == [
        sys.executable,
        "-m",
        "agent_relay.server",
        "--host",
        "127.0.0.1",
        "--port",
        "23456",
    ]
    assert agent_command(23456, tmp_path) == [
        sys.executable,
        "-m",
        "agent_relay.agent",
    ]


def test_shared_commands_keep_server_environment_mode_when_installed_runtime_is_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installed = str(tmp_path / "agent-relay")
    monkeypatch.setenv("RELAY_E2E_AGENT_RELAY_COMMAND", installed)

    assert server_command(23456) == [
        sys.executable,
        "-m",
        "agent_relay.server",
        "--host",
        "127.0.0.1",
        "--port",
        "23456",
    ]
    assert agent_command(23456, tmp_path) == [installed, "agent"]


def test_shared_evidence_writer_rejects_preexisting_symlink(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text("unchanged", encoding="utf-8")
    try:
        (evidence / "output.log").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this host")

    with pytest.raises((E2EError, FileExistsError, OSError, ValueError)):
        write_artifact(evidence, "output.log", b"bounded\n")

    assert outside.read_text(encoding="utf-8") == "unchanged"


def test_shared_artifact_directory_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    link = tmp_path / "evidence"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this host")

    with pytest.raises(E2EError, match="unsafe evidence directory"):
        prepare_artifact_directory(link)


def test_shared_evidence_writer_rejects_oversized_payload(tmp_path: Path) -> None:
    with pytest.raises(E2EError, match="oversized"):
        write_artifact(tmp_path, "output.log", b"x" * 4097)


def test_runtime_scenarios_import_without_the_tests_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def reject_tests_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "tests" or name.startswith("tests."):
            raise ModuleNotFoundError("tests package is unavailable at runtime")
        return real_import(name, globals, locals, fromlist, level)

    for name in tuple(sys.modules):
        if name.startswith("scripts.e2e.scenarios"):
            sys.modules.pop(name)
    monkeypatch.setattr(builtins, "__import__", reject_tests_import)

    scenarios = importlib.import_module("scripts.e2e.scenarios")

    assert callable(scenarios.run_core_scenario)
    assert callable(scenarios.run_browser_scenario)


def test_happy_path_owns_reconnect_restart_evidence_and_cleanup_order() -> None:
    adapter = FakeAdapter()

    run_lifecycle(adapter, lambda: scenario(adapter))

    assert adapter.events == [
        "prepare",
        "server:start",
        "server:ready",
        "agent:start",
        "agent:ready",
        "scenario",
        "agent:stop",
        "agent:stopped",
        "agent:start",
        "agent:ready",
        "scenario",
        "server:stop",
        "server:stopped",
        "server:start",
        "server:ready",
        "agent:ready",
        "scenario",
        "evidence",
        "cleanup",
    ]


@pytest.mark.parametrize("failure_event", ["prepare", "server:start", "agent:start"])
def test_startup_failure_still_runs_cleanup(failure_event: str) -> None:
    primary = RuntimeError("startup failed")
    adapter = FakeAdapter(failures={failure_event: primary})

    with pytest.raises(RuntimeError) as raised:
        run_lifecycle(adapter, lambda: scenario(adapter))

    assert raised.value is primary
    assert adapter.events[-1] == "cleanup"
    assert adapter.events.count("cleanup") == 1


def test_scenario_failure_still_runs_cleanup_and_skips_evidence() -> None:
    primary = ValueError("scenario failed")
    adapter = FakeAdapter(failures={"scenario": primary})

    with pytest.raises(ValueError) as raised:
        run_lifecycle(adapter, lambda: scenario(adapter))

    assert raised.value is primary
    assert adapter.events[-1] == "cleanup"
    assert "evidence" not in adapter.events


def test_cleanup_failure_is_reported_without_a_primary_failure() -> None:
    cleanup = RuntimeError("cleanup failed")
    adapter = FakeAdapter(failures={"cleanup": cleanup})

    with pytest.raises(RuntimeError) as raised:
        run_lifecycle(adapter, lambda: scenario(adapter))

    assert raised.value is cleanup


def test_primary_failure_wins_over_cleanup_failure() -> None:
    primary = ValueError("scenario failed")
    cleanup = RuntimeError("cleanup failed")
    adapter = FakeAdapter(failures={"scenario": primary, "cleanup": cleanup})

    with pytest.raises(ValueError) as raised:
        run_lifecycle(adapter, lambda: scenario(adapter))

    assert raised.value is primary
    assert adapter.events[-1] == "cleanup"


def test_reconnect_failure_occurs_after_the_first_agent_stop() -> None:
    primary = RuntimeError("reconnect failed")
    adapter = FakeAdapter(failures={"agent:ready": primary})
    ready_calls = 0

    def fail_only_second_ready() -> None:
        nonlocal ready_calls
        ready_calls += 1
        adapter.events.append("agent:ready")
        if ready_calls == 2:
            raise primary

    adapter.agent.wait_ready = fail_only_second_ready  # type: ignore[method-assign]

    with pytest.raises(RuntimeError) as raised:
        run_lifecycle(adapter, lambda: scenario(adapter))

    assert raised.value is primary
    assert adapter.events[:10] == [
        "prepare",
        "server:start",
        "server:ready",
        "agent:start",
        "agent:ready",
        "scenario",
        "agent:stop",
        "agent:stopped",
        "agent:start",
        "agent:ready",
    ]
    assert adapter.events[-1] == "cleanup"


def test_restart_waits_for_server_and_agent_before_third_scenario() -> None:
    adapter = FakeAdapter()
    scenario_positions: list[int] = []

    def observed_scenario() -> None:
        scenario_positions.append(len(adapter.events))
        scenario(adapter)

    run_lifecycle(adapter, observed_scenario)

    third = scenario_positions[2]
    assert adapter.events[third - 2 : third] == ["server:ready", "agent:ready"]


@dataclass
class FakeProcess:
    label: str
    stopped: bool = False
    returncode: int | None = None
    pid: int = 12345

    def poll(self) -> int | None:
        return 0 if self.stopped else None


@dataclass
class FakeProcessPlatform:
    events: list[str] = field(default_factory=list)
    name: str = "Test"
    device_id: str = "test-e2e-agent"
    run_prefix: str = "test"

    def prepare(self) -> None:
        self.events.append("platform:prepare")

    def minimal_environment(
        self,
        home: Path,
        values: dict[str, str],
    ) -> dict[str, str]:
        return {"HOME": str(home), **values}

    def spawn(
        self,
        argv: list[str],
        *,
        environment: dict[str, str],
        cwd: Path,
        label: str,
    ) -> FakeProcess:
        assert argv
        assert environment
        assert cwd.is_absolute()
        self.events.append(f"{label}:spawn")
        return FakeProcess(label)

    def stop(self, process: FakeProcess) -> None:
        self.events.append(f"{process.label}:stop")
        process.stopped = True
        process.returncode = 0

    def expected_pwd(self, workspace: Path) -> str:
        return str(workspace)

    def cleanup(self) -> None:
        self.events.append("platform:cleanup")


def test_terminal_adapter_runs_the_same_scenario_three_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform = FakeProcessPlatform()
    runtimes: list[object] = []
    endpoint_results = iter((False, True))

    monkeypatch.setattr(terminal, "_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        terminal,
        "_server_endpoint_available",
        lambda *_args, **_kwargs: next(endpoint_results),
    )

    def run_core(runtime: object, *_args: object, **_kwargs: object) -> None:
        runtimes.append(runtime)
        platform.events.append("scenario")

    monkeypatch.setattr(terminal.scenarios, "run_core_scenario", run_core)

    run_terminal_e2e(platform)

    assert len(runtimes) == 3
    assert runtimes[0] is runtimes[1] is runtimes[2]
    assert platform.events == [
        "platform:prepare",
        "server-initial:spawn",
        "agent-initial:spawn",
        "scenario",
        "agent-initial:stop",
        "agent-reconnect:spawn",
        "scenario",
        "server-initial:stop",
        "server-restart:spawn",
        "scenario",
        "platform:cleanup",
    ]


def _install_terminal_success_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint_results = iter((False, True))
    monkeypatch.setattr(terminal, "_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        terminal,
        "_server_endpoint_available",
        lambda *_args, **_kwargs: next(endpoint_results),
    )
    monkeypatch.setattr(
        terminal.scenarios,
        "run_core_scenario",
        lambda *_args, **_kwargs: None,
    )


def test_terminal_evidence_is_published_only_after_successful_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    platform = FakeProcessPlatform()
    _install_terminal_success_fakes(monkeypatch)
    evidence = tmp_path / "evidence"

    run_terminal_e2e(
        platform,
        evidence_dir=evidence,
        output_file=evidence / "output.log",
    )

    assert (evidence / "output.log").read_text(encoding="ascii") == (
        "Test MCP end-to-end scenario passed.\n"
    )
    assert (evidence / "success.json").read_text(encoding="ascii") == (
        '{"status":"passed"}\n'
    )
    assert platform.events[-1] == "platform:cleanup"


def test_cleanup_failure_never_publishes_success_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    platform = FakeProcessPlatform()
    _install_terminal_success_fakes(monkeypatch)
    evidence = tmp_path / "evidence"

    def fail_cleanup() -> None:
        platform.events.append("platform:cleanup")
        raise RuntimeError("secret cleanup detail")

    monkeypatch.setattr(platform, "cleanup", fail_cleanup)

    with pytest.raises(E2EError, match="cleanup failed"):
        run_terminal_e2e(
            platform,
            evidence_dir=evidence,
            output_file=evidence / "output.log",
        )

    assert not (evidence / "success.json").exists()
    output = (evidence / "output.log").read_text(encoding="ascii")
    assert "Test E2E cleanup failed." in output
    assert "secret cleanup detail" not in output

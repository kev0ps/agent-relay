from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.e2e import cua, scenarios
from scripts.e2e.common import E2EError


@dataclass
class FakeProcess:
    label: str
    stopped: bool = False
    pid: int = 1000
    returncode: int | None = None

    def poll(self) -> int | None:
        return 0 if self.stopped else None


@dataclass
class FakePlatform:
    events: list[str] = field(default_factory=list)
    name: str = "Test"
    device_id: str = "test-cua-agent"
    run_prefix: str = "test-cua"
    cua_run_prefix: str = "test-cua"
    processes: list[FakeProcess] = field(default_factory=list)

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
        assert argv and environment and cwd.is_absolute()
        process = FakeProcess(label=label, pid=1000 + len(self.processes))
        self.processes.append(process)
        self.events.append(f"{label}:spawn")
        return process

    def stop(self, process: FakeProcess) -> None:
        process.stopped = True
        process.returncode = 0
        self.events.append(f"{process.label}:stop")

    def expected_pwd(self, workspace: Path) -> str:
        return str(workspace)

    def cleanup(self) -> None:
        for process in self.processes:
            process.stopped = True
        self.events.append("platform:cleanup")


@dataclass
class FakeGraphicalSession:
    events: list[str]

    def prepare(
        self,
        platform: FakePlatform,
        *,
        root: Path,
        home: Path,
        repository: Path,
    ) -> dict[str, str]:
        assert root.is_absolute() and home.is_absolute() and repository.is_absolute()
        self.events.append("graphics:prepare")
        return platform.minimal_environment(home, {"DISPLAY": "fixture"})


def _install_cua_success_fakes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    events: list[str],
) -> None:
    chrome = tmp_path / "chrome"
    chrome.write_bytes(b"binary")
    monkeypatch.setattr(cua, "find_chrome", lambda: chrome)
    monkeypatch.setattr(cua, "_fixture_ready", lambda _url: True)
    monkeypatch.setattr(cua, "_status", lambda *_args, **_kwargs: None)

    def run_browser(*_args: object, **_kwargs: object) -> None:
        events.append("browser:scenario")

    monkeypatch.setattr(cua.scenarios, "run_browser_scenario", run_browser)


def test_cua_capabilities_are_unique_and_registry_sorted() -> None:
    assert len(cua.CUA_CAPABILITIES) == len(set(cua.CUA_CAPABILITIES))
    assert cua.CUA_CAPABILITIES == tuple(sorted(cua.CUA_CAPABILITIES))


def test_cua_agent_tools_match_the_shared_public_scenario() -> None:
    assert scenarios.CUA_MCP_TOOLS == (
        "relay_device_status",
        *cua.CUA_AGENT_TOOLS,
    )


def test_shared_cua_runner_owns_setup_scenario_evidence_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    platform = FakePlatform(events=events)
    session = FakeGraphicalSession(events)
    _install_cua_success_fakes(monkeypatch, tmp_path, events)
    evidence = tmp_path / "evidence"

    cua.run_cua_e2e(
        platform,
        session,
        evidence_dir=evidence,
        output_file=evidence / "output.log",
    )

    assert events == [
        "platform:prepare",
        "graphics:prepare",
        "server:spawn",
        "fixture:spawn",
        "agent:spawn",
        "browser:scenario",
        "platform:cleanup",
    ]
    assert (evidence / "success.json").exists()
    assert "Test CUA smoke scenario passed." in (
        evidence / "output.log"
    ).read_text(encoding="ascii")


def test_shared_cua_runner_preserves_primary_error_and_always_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    platform = FakePlatform(events=events)
    session = FakeGraphicalSession(events)
    _install_cua_success_fakes(monkeypatch, tmp_path, events)
    primary = ValueError("browser failed")
    monkeypatch.setattr(
        cua.scenarios,
        "run_browser_scenario",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(primary),
    )
    evidence = tmp_path / "evidence"

    with pytest.raises(ValueError) as raised:
        cua.run_cua_e2e(
            platform,
            session,
            evidence_dir=evidence,
            output_file=evidence / "output.log",
        )

    assert raised.value is primary
    assert events[-1] == "platform:cleanup"
    assert not (evidence / "success.json").exists()
    assert "browser failed" not in (evidence / "output.log").read_text(
        encoding="ascii"
    )


def test_shared_cua_runner_rejects_a_dead_owned_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    platform = FakePlatform(events=events)
    session = FakeGraphicalSession(events)
    _install_cua_success_fakes(monkeypatch, tmp_path, events)

    def stop_fixture(*_args: object, **_kwargs: object) -> None:
        platform.processes[1].stopped = True
        events.append("browser:scenario")

    monkeypatch.setattr(cua.scenarios, "run_browser_scenario", stop_fixture)

    with pytest.raises(E2EError, match="owned process exited"):
        cua.run_cua_e2e(platform, session)

"""Observable lifecycle shared by native Agent Relay E2E adapters."""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Protocol


class ManagedService(Protocol):
    """One restartable service owned by a platform adapter."""

    def start(self) -> None: ...

    def wait_ready(self) -> None: ...

    def stop(self) -> None: ...

    def wait_stopped(self) -> None: ...


class LifecycleAdapter(Protocol):
    """Small adapter contract; the shared runner owns all operation ordering."""

    @property
    def server(self) -> ManagedService: ...

    @property
    def agent(self) -> ManagedService: ...

    def prepare(self) -> None: ...

    def collect_evidence(self) -> None: ...

    def cleanup(self) -> None: ...


def run_lifecycle(adapter: LifecycleAdapter, scenario: Callable[[], None]) -> None:
    """Run initial, agent-reconnect, and server-restart scenario passes.

    Cleanup always runs exactly once. If both the lifecycle body and cleanup
    fail, the body error remains primary; a cleanup-only failure is raised.
    """
    primary_error: BaseException | None = None
    primary_traceback: TracebackType | None = None
    try:
        adapter.prepare()

        adapter.server.start()
        adapter.server.wait_ready()
        adapter.agent.start()
        adapter.agent.wait_ready()
        scenario()

        adapter.agent.stop()
        adapter.agent.wait_stopped()
        adapter.agent.start()
        adapter.agent.wait_ready()
        scenario()

        adapter.server.stop()
        adapter.server.wait_stopped()
        adapter.server.start()
        adapter.server.wait_ready()
        adapter.agent.wait_ready()
        scenario()

        adapter.collect_evidence()
    except BaseException as error:
        primary_error = error
        primary_traceback = error.__traceback__

    cleanup_error: BaseException | None = None
    try:
        adapter.cleanup()
    except BaseException as error:
        cleanup_error = error

    if primary_error is not None:
        raise primary_error.with_traceback(primary_traceback)
    if cleanup_error is not None:
        raise cleanup_error

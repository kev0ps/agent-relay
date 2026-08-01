"""Contract tests for the portable MCP end-to-end kernel.

These tests pin the boundary between the portable scenario/oracle code
and the platform-specific orchestration in native harnesses.

The kernel is loaded by absolute path to avoid forcing a
``tests/__init__.py`` package layout (the existing test layout relies on
pytest's rootdir-based collection only).
"""

from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import sys
from pathlib import Path
from types import ModuleType

import pytest

E2E_DIR = Path(__file__).resolve().parent / "e2e"
_LINUX_COMPUTER_IDENTITY = ("relay-desktop-fixture", "Relay Desktop Fixture")
_WINDOWS_COMPUTER_IDENTITY = (
    "powershell",
    "Agent Relay Computer Use Windows Fixture",
)


# --- Helpers ---------------------------------------------------------------


def _load_e2e(rel_filename: str) -> ModuleType:
    """Load a module under ``tests/e2e/`` by file name and cache it.

    Returns the same module object across calls so attribute access
    reflects the actual module state.
    """
    if not rel_filename.endswith(".py"):
        rel_filename = f"{rel_filename}.py"
    dotted = f"tests.e2e.{Path(rel_filename).stem}" if rel_filename != "__init__.py" else "tests.e2e"

    cached = sys.modules.get(dotted)
    if cached is not None:
        return cached

    target = E2E_DIR / rel_filename
    spec = importlib.util.spec_from_file_location(dotted, target)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {dotted} from {target}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = module
    spec.loader.exec_module(module)
    return module


def _scenarios() -> ModuleType:
    return _load_e2e("scenarios.py")


class _ComputerScenarioResult:
    def __init__(
        self,
        *,
        generation: str | None = None,
        is_error: bool = False,
    ) -> None:
        self.structuredContent = (
            {"generation": generation} if generation is not None else {}
        )
        self.isError = is_error

    def __str__(self) -> str:
        return "rejected"


class _ComputerScenarioSession:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.capture_count = 0

    async def __aenter__(self) -> "_ComputerScenarioSession":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def list_tools(self) -> tuple[str, ...]:
        return _scenarios().EXPECTED_MCP_TOOLS

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, object],
    ) -> _ComputerScenarioResult:
        if tool_name == "relay_computer_capture":
            self.capture_count += 1
            return _ComputerScenarioResult(
                generation=f"generation-{self.capture_count}"
            )
        if (
            tool_name == "relay_computer_click"
            and arguments.get("element_id") == "button-1"
        ):
            return _ComputerScenarioResult(is_error=True)
        return _ComputerScenarioResult()


def _computer_runtime(
    scenarios: ModuleType,
    tmp_path: Path,
    *,
    device_id: str,
    run_id: str,
) -> object:
    return scenarios.RuntimeConfig(
        mcp_url="http://127.0.0.1:8000/mcp",
        control_token="control-token",
        device_id=device_id,
        run_id=run_id,
        fixture_url="http://127.0.0.1:1/",
        fixtures_root=str(tmp_path),
    )


def _install_computer_scenario_fakes(
    monkeypatch: pytest.MonkeyPatch,
    scenarios: ModuleType,
    *,
    session_type: type[_ComputerScenarioSession] = _ComputerScenarioSession,
) -> list[tuple[object, object]]:
    observed: list[tuple[object, object]] = []

    def validate_capture(_result: object, **kwargs: object) -> tuple[str, str]:
        observed.append(
            (kwargs.get("expected_app"), kwargs.get("expected_window_title"))
        )
        capture_number = len(observed)
        return f"field-{capture_number}", f"button-{capture_number}"

    monkeypatch.setattr(scenarios._mcp, "MCPClientSession", session_type)
    monkeypatch.setattr(
        scenarios._oracles,
        "validate_status",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        scenarios._oracles,
        "validate_computer_capture",
        validate_capture,
    )
    monkeypatch.setattr(
        scenarios._oracles,
        "validate_computer_action",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        scenarios._oracles,
        "validate_computer_event",
        lambda *_args, **_kwargs: b"stable-event",
    )
    monkeypatch.setattr(scenarios.time, "sleep", lambda _seconds: None)
    return observed


# --- Module-level portability guard -----------------------------------------
#
# The portable kernel MUST NOT import Docker, the Windows-specific APIs we
# guard behind ``_windows_gate``, or any subprocess shell. We walk the
# loaded modules' ``sys.modules`` footprint and forbid these names.


_PLATFORM_FORBIDDEN_TOP_LEVELS: tuple[str, ...] = (
    "docker",
    "container_e2e",
    "native_e2e",
    "windows_e2e",
)


def _e2e_file_pairs() -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = [("tests.e2e", "__init__.py")]
    if E2E_DIR.is_dir():
        for child in sorted(E2E_DIR.glob("*.py")):
            if child.name == "__init__.py":
                continue
            pairs.append((f"tests.e2e.{child.stem}", child.name))
    return pairs


def test_scenarios_module_exists_and_exposes_typed_runtime_config() -> None:
    """``tests/e2e/scenarios.py`` exists and exports a ``RuntimeConfig`` type."""
    scenarios = _scenarios()

    assert hasattr(scenarios, "RuntimeConfig"), (
        "tests/e2e/scenarios.py must define a RuntimeConfig type "
        "that captures the runtime inputs the portable kernel needs."
    )


@pytest.mark.parametrize(("dotted_name", "rel_path"), _e2e_file_pairs())
def test_portable_kernel_does_not_import_docker_or_windows_apis(
    dotted_name: str,
    rel_path: str,
) -> None:
    """The portable kernel must be free of Docker and Windows-API imports."""
    module = _load_e2e(rel_path)

    forbidden: set[str] = set(_PLATFORM_FORBIDDEN_TOP_LEVELS)
    for forbidden_root in set(_PLATFORM_FORBIDDEN_TOP_LEVELS):
        for loaded in list(sys.modules):
            top = loaded.split(".", 1)[0]
            if top == forbidden_root:
                forbidden.add(loaded)

    imported: set[str] = set()
    for value in module.__dict__.values():
        mod = getattr(value, "__module__", None)
        if mod:
            imported.add(mod)
        top = getattr(value, "__name__", None)
        if isinstance(top, str):
            imported.add(top)

    leaked = sorted(imported & forbidden)
    assert not leaked, (
        f"{dotted_name} must not depend on platform-specific modules; "
        f"leaked imports: {leaked}"
    )


def test_portable_kernel_rejects_caller_supplied_filesystem_paths() -> None:
    """``RuntimeConfig`` must reject unknown fields (no Path-like inputs)."""
    scenarios = _scenarios()

    with pytest.raises(TypeError):
        scenarios.RuntimeConfig(workspace_path=Path("/etc/passwd"))  # type: ignore[call-arg]


def test_runtime_config_fields_are_typed_and_frozen() -> None:
    """``RuntimeConfig`` must be a frozen, fully typed string-only dataclass."""
    scenarios = _scenarios()
    cfg_cls = scenarios.RuntimeConfig

    assert dataclasses.is_dataclass(cfg_cls)
    assert cfg_cls.__dataclass_params__.frozen is True

    annotations = cfg_cls.__annotations__
    for field in dataclasses.fields(cfg_cls):
        assert field.name in annotations, f"{field.name} is untyped"
    # ``from __future__ import annotations`` makes annotations lazy strings;
    # we accept either ``str`` or the string ``"str"`` here.
    for field_name, field_type in annotations.items():
        assert field_type in (str, "str"), (
            f"{field_name} must be typed as str to keep the kernel "
            f"platform-neutral; got {field_type!r}"
        )


def test_expected_mcp_tools_inventory_is_closed() -> None:
    """The expected MCP tool inventory is a closed, unique tuple."""
    scenarios = _scenarios()
    inventory = scenarios.EXPECTED_MCP_TOOLS
    assert isinstance(inventory, tuple)
    assert len(inventory) == len(set(inventory)), "inventory must be unique"
    for name in inventory:
        assert name.startswith("relay_"), name


def test_portable_kernel_exposes_shared_core_browser_and_computer_scenarios() -> None:
    """The portable kernel owns actions, not platform orchestration."""
    scenarios = _scenarios()

    for name in (
        "run_core_scenario",
        "run_browser_scenario",
        "run_computer_scenario",
    ):
        assert callable(getattr(scenarios, name, None)), (
            f"tests/e2e/scenarios.py must expose {name} for every harness"
        )


@pytest.mark.parametrize(
    "script_name",
    ["linux_browser_e2e.py", "windows_browser_e2e.py"],
)
def test_browser_adapters_delegate_launch_to_playwright_persistent_context(script_name: str) -> None:
    source = (E2E_DIR.parents[1] / "scripts" / script_name).read_text(encoding="utf-8")

    assert "RELAY_AGENT_BROWSER_USER_DATA_DIR" in source
    assert "RELAY_AGENT_BROWSER_ALLOWED_ORIGINS" in source
    assert "connect_over_cdp" not in source
    assert "remote-debugging" not in source
    assert "portable_browser_cdp" not in source


def test_computer_scenario_requires_harness_fixture_identity() -> None:
    """The portable CUA scenario must not imply one platform's fixture."""
    parameters = inspect.signature(_scenarios().run_computer_scenario).parameters

    assert parameters["expected_computer_app"].default is inspect.Parameter.empty
    assert (
        parameters["expected_computer_window_title"].default
        is inspect.Parameter.empty
    )


def test_core_scenario_accepts_harness_workspace_pwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core terminal oracle must accept the harness-specific workspace."""
    scenarios = _scenarios()
    observed: list[tuple[str, str]] = []

    class FakeSession:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def list_tools(self) -> tuple[str, ...]:
            return scenarios.EXPECTED_MCP_TOOLS

        async def call(self, _tool_name: str, arguments: dict[str, str]) -> object:
            return object()

    def validate_status(_result: object, **_kwargs: object) -> None:
        return None

    def validate_ping(_result: object) -> None:
        return None

    def validate_terminal(
        _result: object,
        *,
        command_id: str,
        expected: str,
    ) -> None:
        observed.append((command_id, expected))

    monkeypatch.setattr(scenarios._mcp, "MCPClientSession", FakeSession)
    monkeypatch.setattr(scenarios._oracles, "validate_status", validate_status)
    monkeypatch.setattr(scenarios._oracles, "validate_ping", validate_ping)
    monkeypatch.setattr(scenarios._oracles, "validate_terminal", validate_terminal)

    runtime = scenarios.RuntimeConfig(
        mcp_url="http://127.0.0.1:8000/mcp",
        control_token="control-token",
        device_id="native-e2e-agent",
        run_id="run-id",
        fixture_url="http://127.0.0.1:8899/",
        fixtures_root="/tmp/fixtures",
    )

    scenarios.run_core_scenario(runtime, expected_pwd="/tmp/native-workspace")

    assert observed == [
        ("pwd", "/tmp/native-workspace"),
        ("git_branch", "relay-e2e-marker"),
    ]


def test_computer_scenario_passes_expected_capabilities_to_status_oracle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A CUA-only Agent must not be validated as a Browser-capable Agent."""
    scenarios = _scenarios()
    observed: list[tuple[str, ...] | None] = []

    def validate_status(_result: object, **kwargs: object) -> None:
        value = kwargs.get("expected_capabilities")
        observed.append(value if isinstance(value, tuple) else None)

    def reject_capture(_result: object, **_kwargs: object) -> tuple[str, str]:
        raise ValueError("stop after status assertion")

    monkeypatch.setattr(scenarios._mcp, "MCPClientSession", _ComputerScenarioSession)
    monkeypatch.setattr(scenarios._oracles, "validate_status", validate_status)
    monkeypatch.setattr(scenarios._oracles, "validate_computer_capture", reject_capture)

    runtime = _computer_runtime(
        scenarios,
        tmp_path,
        device_id="linux-cua-e2e-agent",
        run_id="linux-cua-test",
    )
    expected = (
        "computer.capture",
        "computer.click",
        "computer.type",
        "system.ping",
        "terminal.exec",
    )

    with pytest.raises(ValueError, match="stop after status assertion"):
        scenarios.run_computer_scenario(
            runtime,
            "relay-value",
            expected_capabilities=expected,
            expected_computer_app=_LINUX_COMPUTER_IDENTITY[0],
            expected_computer_window_title=_LINUX_COMPUTER_IDENTITY[1],
        )

    assert observed == [expected]


@pytest.mark.parametrize(
    ("expected_app", "expected_window_title"),
    [
        pytest.param(*_LINUX_COMPUTER_IDENTITY, id="linux"),
        pytest.param(*_WINDOWS_COMPUTER_IDENTITY, id="windows"),
    ],
)
def test_computer_scenario_passes_platform_identity_to_both_capture_oracles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    expected_app: str,
    expected_window_title: str,
) -> None:
    scenarios = _scenarios()
    observed = _install_computer_scenario_fakes(monkeypatch, scenarios)
    runtime = _computer_runtime(
        scenarios,
        tmp_path,
        device_id="portable-cua-e2e-agent",
        run_id="portable-cua-test",
    )

    scenarios.run_computer_scenario(
        runtime,
        "relay-value",
        expected_computer_app=expected_app,
        expected_computer_window_title=expected_window_title,
    )

    expected_identity = (expected_app, expected_window_title)
    assert observed == [expected_identity, expected_identity]


def test_computer_scenario_checks_tools_before_device_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scenarios = _scenarios()
    operations: list[str] = []

    class InventorySession(_ComputerScenarioSession):
        async def list_tools(self) -> tuple[str, ...]:
            operations.append("tools-list")
            return scenarios.EXPECTED_MCP_TOOLS

        async def call(
            self,
            tool_name: str,
            arguments: dict[str, object],
        ) -> _ComputerScenarioResult:
            operations.append(tool_name)
            return await super().call(tool_name, arguments)

    _install_computer_scenario_fakes(
        monkeypatch,
        scenarios,
        session_type=InventorySession,
    )
    runtime = _computer_runtime(
        scenarios,
        tmp_path,
        device_id="portable-cua-e2e-agent",
        run_id="portable-cua-inventory-test",
    )

    scenarios.run_computer_scenario(
        runtime,
        "relay-value",
        expected_computer_app=_LINUX_COMPUTER_IDENTITY[0],
        expected_computer_window_title=_LINUX_COMPUTER_IDENTITY[1],
    )

    assert operations[:2] == ["tools-list", "relay_device_status"]


@pytest.mark.parametrize("inventory_kind", ["missing", "extra", "reordered"])
def test_computer_scenario_rejects_unexpected_tool_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    inventory_kind: str,
) -> None:
    scenarios = _scenarios()
    expected = scenarios.EXPECTED_MCP_TOOLS
    inventories = {
        "missing": expected[:-1],
        "extra": (*expected, "relay_unexpected_tool"),
        "reordered": tuple(reversed(expected)),
    }
    unexpected_inventory = inventories[inventory_kind]

    class UnexpectedInventorySession(_ComputerScenarioSession):
        async def list_tools(self) -> tuple[str, ...]:
            return unexpected_inventory

    _install_computer_scenario_fakes(
        monkeypatch,
        scenarios,
        session_type=UnexpectedInventorySession,
    )
    runtime = _computer_runtime(
        scenarios,
        tmp_path,
        device_id="portable-cua-e2e-agent",
        run_id="portable-cua-inventory-rejection-test",
    )

    with pytest.raises(ValueError, match="unexpected MCP tools"):
        scenarios.run_computer_scenario(
            runtime,
            "relay-value",
            expected_computer_app=_WINDOWS_COMPUTER_IDENTITY[0],
            expected_computer_window_title=_WINDOWS_COMPUTER_IDENTITY[1],
        )

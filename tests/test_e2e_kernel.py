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
import sys
from pathlib import Path
from types import ModuleType

import pytest

E2E_DIR = Path(__file__).resolve().parent / "e2e"


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

    class FakeSession:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def call(self, _tool_name: str, _arguments: dict[str, object]) -> object:
            return object()

    def validate_status(_result: object, **kwargs: object) -> None:
        value = kwargs.get("expected_capabilities")
        observed.append(value if isinstance(value, tuple) else None)

    def reject_capture(_result: object, **_kwargs: object) -> tuple[str, str]:
        raise ValueError("stop after status assertion")

    monkeypatch.setattr(scenarios._mcp, "MCPClientSession", FakeSession)
    monkeypatch.setattr(scenarios._oracles, "validate_status", validate_status)
    monkeypatch.setattr(scenarios._oracles, "validate_computer_capture", reject_capture)

    runtime = scenarios.RuntimeConfig(
        mcp_url="http://127.0.0.1:8000/mcp",
        control_token="control-token",
        device_id="linux-cua-e2e-agent",
        run_id="linux-cua-test",
        fixture_url="http://127.0.0.1:8898/",
        fixtures_root=str(tmp_path),
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
        )

    assert observed == [expected]

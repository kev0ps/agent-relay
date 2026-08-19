#!/usr/bin/env python3
"""Audit Agent Relay's declared, locked, and imported dependencies."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

IMPORT_TO_DISTRIBUTION = {
    "cua_driver": "cua-driver",
    "fastapi": "fastapi",
    "mcp": "mcp",
    "pydantic": "pydantic",
    "starlette": "starlette",
    "typing_extensions": "typing-extensions",
    "uvicorn": "uvicorn",
    "websockets": "websockets",
    "yaml": "pyyaml",
}

# These modules are imported by application code but are intentionally provided
# by a declared framework dependency. They are not Agent Relay's public API.
TRANSITIVE_IMPORT_PROVIDERS = {
    "starlette": "fastapi",
}


def requirement_name(requirement: str) -> str:
    """Return a normalized distribution name from a PEP 508 requirement."""
    name = re.split(r"[<>=!~;\s\[]", requirement, maxsplit=1)[0]
    return name.strip().lower().replace("_", "-")


def load_project(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def _requirements(values: list[str]) -> set[str]:
    return {requirement_name(value) for value in values}


def _package_versions(lock: dict[str, Any]) -> dict[str, str]:
    versions: dict[str, str] = {}
    for package in lock.get("package", []):
        name = package.get("name")
        version = package.get("version")
        if isinstance(name, str) and isinstance(version, str):
            versions.setdefault(name.lower().replace("_", "-"), version)
    return versions


def collect_imports(paths: tuple[Path, ...]) -> set[str]:
    """Collect third-party top-level imports from Python files."""
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    imports: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = (alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level or not node.module:
                    continue
                names = (node.module.split(".", 1)[0],)
            else:
                continue
            imports.update(name for name in names if name not in stdlib)
    return imports


def _python_files(root: Path, *directories: str) -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for directory in directories
            for path in (root / directory).rglob("*.py")
            if path.is_file()
        )
    )


def build_report(root: Path, project: dict[str, Any]) -> dict[str, Any]:
    project_table = project["project"]
    runtime_names = _requirements(project_table.get("dependencies", []))
    optional_tables = project_table.get("optional-dependencies", {})
    optional_names = {
        requirement_name(requirement)
        for requirements in optional_tables.values()
        for requirement in requirements
    }
    dev_tables = project.get("dependency-groups", {})
    development_names = {
        requirement_name(requirement)
        for requirements in dev_tables.values()
        for requirement in requirements
    }
    build_names = _requirements(project.get("build-system", {}).get("requires", []))

    with (root / "uv.lock").open("rb") as stream:
        lock = tomllib.load(stream)
    versions = _package_versions(lock)
    direct_names = runtime_names | optional_names | development_names | build_names

    runtime = {
        name: versions[name]
        for name in sorted(runtime_names)
        if name in versions
    }
    optional = {
        name: versions[name]
        for name in sorted(optional_names)
        if name in versions
    }
    development = {
        name: versions[name]
        for name in sorted(development_names)
        if name in versions
    }
    build = {
        name: versions[name]
        for name in sorted(build_names)
        if name in versions
    }
    transitive = {
        name: version
        for name, version in sorted(versions.items())
        if name not in direct_names and name != requirement_name(project_table["name"])
    }

    imported_modules = collect_imports(_python_files(root, "src/agent_relay"))
    imported_distributions = {
        IMPORT_TO_DISTRIBUTION[module]
        for module in imported_modules
        if module in IMPORT_TO_DISTRIBUTION
    }
    provided_transitively = {
        IMPORT_TO_DISTRIBUTION[module]
        for module in imported_modules
        if module in TRANSITIVE_IMPORT_PROVIDERS
        and TRANSITIVE_IMPORT_PROVIDERS[module] in direct_names
    }
    missing_declarations = sorted(
        imported_distributions - direct_names - provided_transitively
    )
    missing_lock_entries = sorted(
        (runtime_names | optional_names | development_names) - set(versions)
    )
    unexplained = sorted(
        module
        for module in imported_modules
        if module not in IMPORT_TO_DISTRIBUTION
        and module not in TRANSITIVE_IMPORT_PROVIDERS
    )
    unused_runtime = sorted(runtime_names - imported_distributions)

    return {
        "runtime": runtime,
        "optional": optional,
        "development": development,
        "build": build,
        "transitive": transitive,
        "missing_declarations": missing_declarations,
        "missing_lock_entries": missing_lock_entries,
        "unexplained": unexplained,
        "unused_runtime": unused_runtime,
    }


def report_has_failures(report: dict[str, Any]) -> bool:
    return any(
        report[key]
        for key in (
            "missing_declarations",
            "missing_lock_entries",
            "unexplained",
            "unused_runtime",
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Agent Relay repository root",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="return non-zero when the audit finds missing declarations, missing lock entries, unexplained imports, or unused runtime dependencies",
    )
    arguments = parser.parse_args(argv)
    root = arguments.root.resolve()
    report = build_report(root, load_project(root / "pyproject.toml"))
    print(json.dumps(report, indent=2, sort_keys=False))
    if arguments.check and report_has_failures(report):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

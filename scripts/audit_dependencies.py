#!/usr/bin/env python3
"""Check Agent Relay's third-party imports against direct dependencies."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

# Values are the distribution names that satisfy the corresponding imports.
# Starlette is used through FastAPI, which is the direct project dependency.
IMPORT_TO_DECLARATION = {
    "cua_driver": "cua-driver",
    "fastapi": "fastapi",
    "mcp": "mcp",
    "pydantic": "pydantic",
    "starlette": "fastapi",
    "typing_extensions": "typing-extensions",
    "uvicorn": "uvicorn",
    "websockets": "websockets",
    "yaml": "pyyaml",
}


def requirement_name(requirement: str) -> str:
    """Return a normalized distribution name from a PEP 508 requirement."""
    name = re.split(r"[<>=!~;\s\[]", requirement, maxsplit=1)[0]
    return name.strip().lower().replace("_", "-")


def load_project(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        return tomllib.load(stream)


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


def build_report(root: Path, project: dict[str, Any]) -> dict[str, list[str]]:
    project_table = project["project"]
    declared_names = {
        requirement_name(requirement)
        for requirement in project_table.get("dependencies", [])
    }
    declared_names.update(
        requirement_name(requirement)
        for requirements in project_table.get("optional-dependencies", {}).values()
        for requirement in requirements
    )

    imported_modules = collect_imports(_python_files(root, "src/agent_relay"))
    imported_declarations = {
        IMPORT_TO_DECLARATION[module]
        for module in imported_modules
        if module in IMPORT_TO_DECLARATION
    }

    return {
        "missing_declarations": sorted(imported_declarations - declared_names),
        "unexplained": sorted(
            module
            for module in imported_modules
            if module not in IMPORT_TO_DECLARATION
        ),
    }


def report_has_failures(report: dict[str, list[str]]) -> bool:
    return any(report.values())


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
        help="return non-zero when imports are not directly declared",
    )
    arguments = parser.parse_args(argv)
    root = arguments.root.resolve()
    report = build_report(root, load_project(root / "pyproject.toml"))
    print(json.dumps(report, indent=2))
    if arguments.check and report_has_failures(report):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

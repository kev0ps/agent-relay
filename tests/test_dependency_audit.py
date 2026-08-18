from __future__ import annotations

import json
from pathlib import Path

from scripts import audit_dependencies

ROOT = Path(__file__).parents[1]


def test_requirement_name_handles_bounds_and_extras() -> None:
    assert audit_dependencies.requirement_name("FastAPI>=0.115,<1") == "fastapi"
    assert audit_dependencies.requirement_name("cua-driver==0.19.3") == "cua-driver"
    assert audit_dependencies.requirement_name("pyjwt[crypto]>=2.10") == "pyjwt"


def test_runtime_imports_are_mapped_to_declared_distributions() -> None:
    project = audit_dependencies.load_project(ROOT / "pyproject.toml")
    report = audit_dependencies.build_report(ROOT, project)

    assert report["unexplained"] == []
    assert report["unused_runtime"] == []
    assert report["missing_declarations"] == []
    assert report["missing_lock_entries"] == []
    assert report["runtime"] == {
        "fastapi": "0.141.1",
        "mcp": "2.0.0",
        "pydantic": "2.13.4",
        "pyyaml": "6.0.3",
        "typing-extensions": "4.16.0",
        "uvicorn": "0.51.0",
        "websockets": "17.0.1",
    }


def test_report_is_stable_json_and_check_succeeds(capsys) -> None:
    exit_code = audit_dependencies.main(["--root", str(ROOT), "--check"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert list(payload) == [
        "runtime",
        "optional",
        "development",
        "build",
        "transitive",
        "missing_declarations",
        "missing_lock_entries",
        "unexplained",
        "unused_runtime",
    ]
    assert payload["runtime"]["fastapi"] == "0.141.1"
    assert payload["transitive"]["anyio"] == "4.14.2"


def test_check_fails_when_an_imported_distribution_is_not_declared() -> None:
    project = audit_dependencies.load_project(ROOT / "pyproject.toml")
    project["project"] = {
        **project["project"],
        "dependencies": [
            requirement
            for requirement in project["project"]["dependencies"]
            if audit_dependencies.requirement_name(requirement) != "mcp"
        ],
    }

    report = audit_dependencies.build_report(ROOT, project)

    assert "mcp" in report["missing_declarations"]
    assert audit_dependencies.report_has_failures(report)


def test_check_fails_when_a_declared_runtime_distribution_is_not_locked() -> None:
    project = audit_dependencies.load_project(ROOT / "pyproject.toml")
    project["project"] = {
        **project["project"],
        "dependencies": [
            *project["project"]["dependencies"],
            "not-in-uv-lock>=1",
        ],
    }

    report = audit_dependencies.build_report(ROOT, project)

    assert "not-in-uv-lock" in report["missing_lock_entries"]
    assert audit_dependencies.report_has_failures(report)


def test_import_collection_ignores_relative_and_standard_library_imports(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.py"
    source.write_text(
        """
import asyncio
import third_party
from .local import value
from another_package import item
""",
        encoding="utf-8",
    )

    assert audit_dependencies.collect_imports((source,)) == {
        "another_package",
        "third_party",
    }


def test_python_ci_job_runs_lock_and_dependency_audit_gates() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    python_job = workflow.split("  python:", 1)[1].split("\n  container:", 1)[0]

    assert "uv lock --check" in python_job
    assert "uv run --frozen python scripts/audit_dependencies.py --check" in python_job

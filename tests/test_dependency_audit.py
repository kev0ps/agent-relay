from __future__ import annotations

from pathlib import Path

from scripts import audit_dependencies

ROOT = Path(__file__).parents[1]


def test_requirement_name_handles_bounds_and_extras() -> None:
    assert audit_dependencies.requirement_name("FastAPI>=0.115,<1") == "fastapi"
    assert audit_dependencies.requirement_name("cua-driver==0.19.3") == "cua-driver"
    assert audit_dependencies.requirement_name("pyjwt[crypto]>=2.10") == "pyjwt"


def test_application_imports_match_direct_dependency_declarations() -> None:
    project = audit_dependencies.load_project(ROOT / "pyproject.toml")
    report = audit_dependencies.build_report(ROOT, project)

    assert set(report) == {"missing_declarations", "unexplained"}
    assert report["missing_declarations"] == []
    assert report["unexplained"] == []


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

    assert report["missing_declarations"] == ["mcp"]
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

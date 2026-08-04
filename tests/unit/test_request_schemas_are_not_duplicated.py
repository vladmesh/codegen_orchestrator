"""A class name may be defined in `services/api/src/schemas` or in
`shared/contracts/dto`, not in both.

Two classes with one name is how `ProjectUpdate` came to mean different things
on the two ends of a call: the scheduler sent the contract's field set and the
API validated against its own, so the PATCH answered 422 and the project spec
never synced. The API is free to re-export the contract class (an import is not
a definition), which is what a single definition looks like from here.

`KNOWN_DUPLICATES` is the backlog, not the rule: a name may leave the list when
its two definitions are merged, and a name that is not on it fails immediately.
Stale entries fail too, so the list can only shrink on purpose.
"""

import ast
import importlib
from pathlib import Path
import sys

from shared.contracts.dto import project as contract

ROOT = Path(__file__).resolve().parents[2]
API_SCHEMAS = ROOT / "services" / "api" / "src" / "schemas"
CONTRACT_DTOS = ROOT / "shared" / "contracts" / "dto"

# Names still defined twice, each pending its own merge. Do not add to this list.
KNOWN_DUPLICATES = {
    "AnalyticsDailyCreate",
    "AnalyticsHourlyCreate",
    "AnalyticsKnownUserUpsert",
    "AnalyticsKnownUsersBatchUpsert",
    "ApplicationCreate",
    "ApplicationUpdate",
    "IncidentCreate",
    "IncidentUpdate",
    "RepositoryCreate",
    "RepositoryUpdate",
    "RunCreate",
    "ServerCreate",
    "StoryCreate",
    "StoryUpdate",
    "TaskCreate",
    "TaskEventCreate",
    "TaskUpdate",
    "TemporaryAccessGrantCreate",
    "TemporaryAccessGrantUpdate",
}


def _defined_class_names(package: Path) -> dict[str, str]:
    """Class names defined by `class X` in the package, mapped to their file."""
    names: dict[str, str] = {}
    for module in sorted(package.glob("*.py")):
        tree = ast.parse(module.read_text(), filename=str(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                names[node.name] = module.name
    return names


def _duplicated_names() -> set[str]:
    api = _defined_class_names(API_SCHEMAS)
    dto = _defined_class_names(CONTRACT_DTOS)
    assert api, "no schema modules found — the path is wrong"
    assert dto, "no DTO modules found — the path is wrong"
    return set(api) & set(dto)


def test_project_request_schemas_have_one_definition():
    assert not {"ProjectCreate", "ProjectUpdate"} & _duplicated_names()


def test_the_api_serves_the_contract_classes():
    """The API's re-export and the contract must be the same object, not a copy."""
    api_src = str(ROOT / "services" / "api" / "src")
    sys.path.insert(0, api_src)
    try:
        served = importlib.import_module("schemas.project")
    finally:
        sys.path.remove(api_src)

    assert served.ProjectCreate is contract.ProjectCreate
    assert served.ProjectUpdate is contract.ProjectUpdate


def test_no_new_duplicated_schema_names():
    duplicated = _duplicated_names()

    new = duplicated - KNOWN_DUPLICATES
    assert not new, f"new class name defined in both trees: {sorted(new)}"


def test_known_duplicates_list_has_no_stale_entries():
    stale = KNOWN_DUPLICATES - _duplicated_names()
    assert not stale, f"already merged, drop from KNOWN_DUPLICATES: {sorted(stale)}"

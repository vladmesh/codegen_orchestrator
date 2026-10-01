"""Run boundaries accept the canonical vocabulary before anything is persisted."""

from datetime import UTC, datetime

from jsonschema import Draft202012Validator
from pydantic import ValidationError
import pytest

from shared.contracts.dto.run import RunCreate, RunStatus, RunType
from src.schemas.run import RunRead, RunUpdate


@pytest.mark.parametrize("run_type", ["", "build", "DEPLOY", "deploy "])
def test_create_refuses_unknown_run_type(run_type: str) -> None:
    with pytest.raises(ValidationError, match="type"):
        RunCreate(id="run-1", type=run_type)


@pytest.mark.parametrize("run_type", list(RunType))
def test_create_preserves_each_canonical_wire_type(run_type: RunType) -> None:
    run = RunCreate.model_validate({"id": "run-1", "type": run_type.value})

    assert run.model_dump(mode="json")["type"] == run_type.value


@pytest.mark.parametrize("status", [None, "", "done", "RUNNING", "failed "])
def test_update_refuses_unknown_or_explicit_null_status(status: str | None) -> None:
    with pytest.raises(ValidationError, match="status"):
        RunUpdate.model_validate({"status": status})


def test_metadata_update_omits_status_instead_of_clearing_it() -> None:
    update = RunUpdate(run_metadata={"iteration": 2})

    assert update.model_dump(exclude_unset=True, mode="json") == {"run_metadata": {"iteration": 2}}


def test_update_json_schema_allows_omission_but_refuses_null() -> None:
    validator = Draft202012Validator(RunUpdate.model_json_schema())

    assert validator.is_valid({})
    assert validator.is_valid({"status": "running"})
    assert not validator.is_valid({"status": None})


@pytest.mark.parametrize("status", list(RunStatus))
def test_update_preserves_each_canonical_wire_status(status: RunStatus) -> None:
    update = RunUpdate.model_validate({"status": status.value})

    assert update.model_dump(exclude_unset=True, mode="json") == {"status": status.value}


@pytest.mark.parametrize("field,value", [("type", "build"), ("status", "done")])
def test_read_refuses_a_row_outside_the_run_vocabulary(field: str, value: str) -> None:
    payload = {
        "id": "run-1",
        "type": "deploy",
        "status": "queued",
        "created_at": datetime.now(UTC),
        field: value,
    }

    with pytest.raises(ValidationError, match=field):
        RunRead.model_validate(payload)


@pytest.mark.parametrize("run_type", list(RunType))
@pytest.mark.parametrize("status", list(RunStatus))
def test_read_keeps_nullable_project_and_result_semantics(
    run_type: RunType, status: RunStatus
) -> None:
    payload = {
        "id": "run-1",
        "type": run_type.value,
        "status": status.value,
        "project_id": None,
        "result": None,
        "created_at": datetime.now(UTC).isoformat(),
    }

    read = RunRead.model_validate(payload).model_dump(mode="json")

    assert read["type"] == run_type.value
    assert read["status"] == status.value
    assert read["project_id"] is None
    assert read["result"] is None

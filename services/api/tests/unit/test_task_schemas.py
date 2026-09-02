"""Unit tests for Task API schemas (planning layer)."""

from datetime import UTC, datetime
from typing import get_type_hints
import uuid

from pydantic import BaseModel, ValidationError
import pytest

from shared.contracts.dto.task import TaskDTO
from src.schemas.task import (
    TaskCreate,
    TaskEventCreate,
    TaskEventRead,
    TaskRead,
    TaskTransition,
    TaskUpdate,
)

PROJECT_UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")
PROJECT_UUID_2 = uuid.UUID("00000000-0000-0000-0000-000000000002")

#: Sentinel for "this field has no default", so a field defaulting to `None` and
#: a required field never compare equal.
_NO_DEFAULT = object()


def test_task_create_minimal():
    schema = TaskCreate(project_id=PROJECT_UUID, title="Fix login bug")
    assert schema.title == "Fix login bug"
    assert schema.type == "feature"
    assert schema.priority == 0
    assert schema.max_iterations == 3
    assert schema.created_by == "system"
    assert schema.project_id == PROJECT_UUID


def test_task_create_full():
    schema = TaskCreate(
        project_id=PROJECT_UUID_2,
        type="fix",
        title="Fix login bug",
        description="Users can't login with Google",
        acceptance_criteria="Google OAuth works",
        priority=1,
        max_iterations=5,
        created_by="po",
    )
    assert schema.project_id == PROJECT_UUID_2
    assert schema.type == "fix"
    assert schema.max_iterations == 5


def test_task_requires_project_id():
    with pytest.raises(ValidationError):
        TaskCreate(title="Test without project")


def test_task_create_invalid_type():
    with pytest.raises(ValidationError):
        TaskCreate(project_id=PROJECT_UUID, title="Test", type="invalid_type")


def test_task_read_from_attributes():
    now = datetime.now(UTC)

    class FakeModel:
        id = "task-abc"
        project_id = PROJECT_UUID
        type = "feature"
        title = "Test"
        description = None
        plan = None
        status = "backlog"
        priority = 0
        acceptance_criteria = None
        current_iteration = 0
        max_iterations = 3
        created_by = "system"
        # Non-nullable on the model, required on the schema: a task that was
        # never planned against a Product Brief is admitted by existing.
        dispatch_admitted = True
        created_at = now
        updated_at = now

    read = TaskRead.model_validate(FakeModel(), from_attributes=True)
    assert read.id == "task-abc"
    assert read.status == "backlog"
    assert read.plan is None
    assert read.last_event is None
    assert read.elapsed_minutes is None


def test_task_read_with_plan():
    now = datetime.now(UTC)

    class FakeModel:
        id = "task-abc"
        project_id = PROJECT_UUID
        type = "feature"
        title = "Test"
        description = None
        plan = "## Step 1\nDo the thing"
        status = "in_dev"
        priority = 0
        acceptance_criteria = None
        current_iteration = 0
        max_iterations = 3
        created_by = "system"
        # Non-nullable on the model, required on the schema: a task that was
        # never planned against a Product Brief is admitted by existing.
        dispatch_admitted = True
        created_at = now
        updated_at = now

    read = TaskRead.model_validate(FakeModel(), from_attributes=True)
    assert read.plan == "## Step 1\nDo the thing"


def test_task_create_with_need_e2e():
    schema = TaskCreate(project_id=PROJECT_UUID, title="Complex task", need_e2e=True)
    assert schema.need_e2e is True


def test_task_create_need_e2e_defaults_false():
    schema = TaskCreate(project_id=PROJECT_UUID, title="Simple task")
    assert schema.need_e2e is False


def test_task_read_includes_need_e2e():
    now = datetime.now(UTC)

    class FakeModel:
        id = "task-abc"
        project_id = PROJECT_UUID
        type = "feature"
        title = "Test"
        description = None
        plan = None
        status = "backlog"
        priority = 0
        acceptance_criteria = None
        current_iteration = 0
        max_iterations = 3
        created_by = "system"
        # Non-nullable on the model, required on the schema: a task that was
        # never planned against a Product Brief is admitted by existing.
        dispatch_admitted = True
        need_e2e = True
        created_at = now
        updated_at = now

    read = TaskRead.model_validate(FakeModel(), from_attributes=True)
    assert read.need_e2e is True


def test_task_update_need_e2e():
    update = TaskUpdate(need_e2e=True)
    data = update.model_dump(exclude_unset=True)
    assert data == {"need_e2e": True}


def test_task_update_partial():
    update = TaskUpdate(title="New title")
    data = update.model_dump(exclude_unset=True)
    assert data == {"title": "New title"}
    assert "description" not in data


def test_task_update_with_plan():
    update = TaskUpdate(plan="## Plan\nStep 1: Do thing")
    data = update.model_dump(exclude_unset=True)
    assert data == {"plan": "## Plan\nStep 1: Do thing"}


def test_task_update_with_project_id():
    update = TaskUpdate(project_id=uuid.UUID("00000000-0000-0000-0000-000000000003"))
    data = update.model_dump(exclude_unset=True)
    assert data == {"project_id": uuid.UUID("00000000-0000-0000-0000-000000000003")}


def test_task_update_current_iteration():
    update = TaskUpdate(current_iteration=2)
    data = update.model_dump(exclude_unset=True)
    assert data == {"current_iteration": 2}


def test_task_transition():
    t = TaskTransition(reason="CI failed", actor="system")
    assert t.reason == "CI failed"
    assert t.details == {}


def test_task_event_create():
    event = TaskEventCreate(
        event_type="iteration_start",
        iteration=0,
        details={"run_id": "eng-111"},
        actor="system",
    )
    assert event.event_type == "iteration_start"
    assert event.iteration == 0


def test_task_event_create_comment():
    event = TaskEventCreate(
        event_type="comment",
        details={"text": "Looks good, proceeding with deploy"},
        actor="engineer",
    )
    assert event.event_type == "comment"
    assert event.details["text"] == "Looks good, proceeding with deploy"


def test_task_event_create_step_start_rejected():
    """step_start was removed from valid event types."""
    with pytest.raises(ValidationError):
        TaskEventCreate(event_type="step_start")


def test_task_event_create_step_done_rejected():
    """step_done was removed from valid event types."""
    with pytest.raises(ValidationError):
        TaskEventCreate(event_type="step_done")


def test_task_event_create_invalid_type():
    with pytest.raises(ValidationError):
        TaskEventCreate(event_type="invalid_event")


def test_task_event_read():
    now = datetime.now(UTC)
    read = TaskEventRead(
        id=1,
        task_id="task-abc",
        event_type="status_change",
        from_status="backlog",
        to_status="todo",
        iteration=None,
        details={},
        actor="po",
        created_at=now,
    )
    assert read.from_status == "backlog"
    assert read.to_status == "todo"


class TestTaskReadPairing:
    """`TaskRead` and the shared `TaskDTO` are one response contract in two files.

    Every client that reads a task — `services/scheduler`, `services/langgraph` —
    parses the response through `TaskDTO`, which ignores fields it does not
    declare and applies its own defaults to fields the response omits. A field
    that exists on one model alone, or is typed differently, or is required on
    one side and optional on the other, is a contract split that no runtime
    fallback may paper over. The Story pair learned this the expensive way; the
    guard is the same one, over the Task pair, because `dispatch_admitted` is
    exactly the kind of field a silent default would invert.
    """

    @staticmethod
    def _field_specs(model: type[BaseModel]) -> dict[str, tuple[object, bool, object]]:
        hints = get_type_hints(model)

        def default_of(field) -> object:
            if field.is_required():
                return _NO_DEFAULT
            return field.get_default(call_default_factory=True)

        return {
            name: (hints[name], field.is_required(), default_of(field))
            for name, field in model.model_fields.items()
        }

    def test_both_models_declare_the_same_fields(self):
        assert set(TaskDTO.model_fields) == set(TaskRead.model_fields)

    #: Fields whose specs already differed before the Product Brief admission
    #: existed: `TaskDTO` types the two vocabulary fields as enums while
    #: `TaskRead` returns their strings, and it defaults two optional strings
    #: `TaskRead` requires. Narrowing that drift is a response-contract change
    #: with its own consumers, so it is frozen here rather than widened: this
    #: list may shrink, and a new name in it is a new split.
    _KNOWN_DIVERGENCES = frozenset({"type", "status", "description", "acceptance_criteria"})

    def test_no_new_field_splits_the_contract(self):
        """Every field but the frozen ones has the same spec on both sides."""
        dto = self._field_specs(TaskDTO)
        read = self._field_specs(TaskRead)
        compared = set(dto) - self._KNOWN_DIVERGENCES
        assert {name: dto[name] for name in compared} == {name: read[name] for name in compared}

    def test_the_frozen_divergences_are_all_real(self):
        """A name that stops diverging has to leave the list, not linger in it."""
        dto = self._field_specs(TaskDTO)
        read = self._field_specs(TaskRead)
        assert {name for name in self._KNOWN_DIVERGENCES if dto[name] != read[name]} == (
            self._KNOWN_DIVERGENCES
        )

    def test_the_dispatch_admission_is_required_on_both_sides(self):
        """Spelled out: a default here would invent dispatch authority.

        A response that omitted the field would be read as "admitted", which is
        the one thing the coverage-to-dispatch boundary exists to withhold.
        """
        for model in (TaskDTO, TaskRead):
            field = model.model_fields["dispatch_admitted"]
            assert get_type_hints(model)["dispatch_admitted"] is bool
            assert field.is_required(), f"{model.__name__}.dispatch_admitted must have no default"


def test_task_dto_refuses_a_response_without_the_dispatch_admission():
    """No producer may omit it, so an omission is a broken response, not a default."""
    response = {
        "id": "task-abc",
        "project_id": str(PROJECT_UUID),
        "type": "feature",
        "title": "Test",
        "status": "todo",
        "priority": 0,
        "current_iteration": 0,
        "max_iterations": 3,
        "created_by": "system",
        "created_at": datetime.now(UTC).isoformat(),
    }
    with pytest.raises(ValidationError):
        TaskDTO.model_validate(response)
    assert TaskDTO.model_validate({**response, "dispatch_admitted": False}).dispatch_admitted is (
        False
    )

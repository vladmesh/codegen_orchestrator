"""Unit tests for Task API schemas (planning layer)."""

from datetime import UTC, datetime

from pydantic import ValidationError
import pytest

from src.schemas.task import (
    TaskCreate,
    TaskEventCreate,
    TaskEventRead,
    TaskRead,
    TaskTransition,
    TaskUpdate,
)


def test_task_create_minimal():
    schema = TaskCreate(project_id="proj-1", title="Fix login bug")
    assert schema.title == "Fix login bug"
    assert schema.type == "feature"
    assert schema.priority == 0
    assert schema.max_iterations == 3
    assert schema.created_by == "system"
    assert schema.project_id == "proj-1"


def test_task_create_full():
    schema = TaskCreate(
        project_id="proj-123",
        type="fix",
        title="Fix login bug",
        description="Users can't login with Google",
        acceptance_criteria="Google OAuth works",
        priority=1,
        max_iterations=5,
        created_by="po",
    )
    assert schema.project_id == "proj-123"
    assert schema.type == "fix"
    assert schema.max_iterations == 5


def test_task_create_with_milestone_id():
    schema = TaskCreate(project_id="proj-1", title="Task in milestone", milestone_id="ms-abc")
    assert schema.milestone_id == "ms-abc"


def test_task_create_milestone_id_optional():
    schema = TaskCreate(project_id="proj-1", title="No milestone")
    assert schema.milestone_id is None


def test_task_requires_project_id():
    with pytest.raises(ValidationError):
        TaskCreate(title="Test without project")


def test_task_create_invalid_type():
    with pytest.raises(ValidationError):
        TaskCreate(project_id="proj-1", title="Test", type="invalid_type")


def test_task_read_from_attributes():
    now = datetime.now(UTC)

    class FakeModel:
        id = "task-abc"
        project_id = "proj-1"
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
        project_id = "proj-1"
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
        created_at = now
        updated_at = now

    read = TaskRead.model_validate(FakeModel(), from_attributes=True)
    assert read.plan == "## Step 1\nDo the thing"


def test_task_create_with_need_e2e():
    schema = TaskCreate(project_id="proj-1", title="Complex task", need_e2e=True)
    assert schema.need_e2e is True


def test_task_create_need_e2e_defaults_false():
    schema = TaskCreate(project_id="proj-1", title="Simple task")
    assert schema.need_e2e is False


def test_task_read_includes_need_e2e():
    now = datetime.now(UTC)

    class FakeModel:
        id = "task-abc"
        project_id = "proj-1"
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


def test_task_update_with_milestone_id():
    update = TaskUpdate(milestone_id="ms-abc")
    data = update.model_dump(exclude_unset=True)
    assert data == {"milestone_id": "ms-abc"}


def test_task_update_with_plan():
    update = TaskUpdate(plan="## Plan\nStep 1: Do thing")
    data = update.model_dump(exclude_unset=True)
    assert data == {"plan": "## Plan\nStep 1: Do thing"}


def test_task_update_with_project_id():
    update = TaskUpdate(project_id="proj-new")
    data = update.model_dump(exclude_unset=True)
    assert data == {"project_id": "proj-new"}


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

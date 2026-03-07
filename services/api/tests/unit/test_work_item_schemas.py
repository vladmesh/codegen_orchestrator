"""Unit tests for WorkItem API schemas."""

from datetime import UTC, datetime

from pydantic import ValidationError
import pytest

from src.schemas.work_item import (
    WorkItemCreate,
    WorkItemEventCreate,
    WorkItemEventRead,
    WorkItemRead,
    WorkItemTransition,
    WorkItemUpdate,
)


def test_work_item_create_minimal():
    schema = WorkItemCreate(project_id="proj-1", title="Fix login bug")
    assert schema.title == "Fix login bug"
    assert schema.type == "feature"
    assert schema.priority == 0
    assert schema.max_iterations == 3
    assert schema.created_by == "system"
    assert schema.project_id == "proj-1"


def test_work_item_create_full():
    schema = WorkItemCreate(
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


def test_work_item_create_with_milestone_id():
    schema = WorkItemCreate(project_id="proj-1", title="Task in milestone", milestone_id="ms-abc")
    assert schema.milestone_id == "ms-abc"


def test_work_item_create_milestone_id_optional():
    schema = WorkItemCreate(project_id="proj-1", title="No milestone")
    assert schema.milestone_id is None


def test_work_item_requires_project_id():
    with pytest.raises(ValidationError):
        WorkItemCreate(title="Test without project")


def test_work_item_create_invalid_type():
    with pytest.raises(ValidationError):
        WorkItemCreate(project_id="proj-1", title="Test", type="invalid_type")


def test_work_item_read_from_attributes():
    now = datetime.now(UTC)

    class FakeModel:
        id = "wi-abc"
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

    read = WorkItemRead.model_validate(FakeModel(), from_attributes=True)
    assert read.id == "wi-abc"
    assert read.status == "backlog"
    assert read.plan is None
    assert read.last_event is None
    assert read.elapsed_minutes is None


def test_work_item_read_with_plan():
    now = datetime.now(UTC)

    class FakeModel:
        id = "wi-abc"
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

    read = WorkItemRead.model_validate(FakeModel(), from_attributes=True)
    assert read.plan == "## Step 1\nDo the thing"


def test_work_item_update_partial():
    update = WorkItemUpdate(title="New title")
    data = update.model_dump(exclude_unset=True)
    assert data == {"title": "New title"}
    assert "description" not in data


def test_work_item_update_with_milestone_id():
    update = WorkItemUpdate(milestone_id="ms-abc")
    data = update.model_dump(exclude_unset=True)
    assert data == {"milestone_id": "ms-abc"}


def test_work_item_update_with_plan():
    update = WorkItemUpdate(plan="## Plan\nStep 1: Do thing")
    data = update.model_dump(exclude_unset=True)
    assert data == {"plan": "## Plan\nStep 1: Do thing"}


def test_work_item_update_with_project_id():
    update = WorkItemUpdate(project_id="proj-new")
    data = update.model_dump(exclude_unset=True)
    assert data == {"project_id": "proj-new"}


def test_work_item_transition():
    t = WorkItemTransition(reason="CI failed", actor="system")
    assert t.reason == "CI failed"
    assert t.details == {}


def test_work_item_event_create():
    event = WorkItemEventCreate(
        event_type="iteration_start",
        iteration=0,
        details={"task_id": "eng-111"},
        actor="system",
    )
    assert event.event_type == "iteration_start"
    assert event.iteration == 0


def test_work_item_event_create_comment():
    event = WorkItemEventCreate(
        event_type="comment",
        details={"text": "Looks good, proceeding with deploy"},
        actor="engineer",
    )
    assert event.event_type == "comment"
    assert event.details["text"] == "Looks good, proceeding with deploy"


def test_work_item_event_create_step_start_rejected():
    """step_start was removed from valid event types."""
    with pytest.raises(ValidationError):
        WorkItemEventCreate(event_type="step_start")


def test_work_item_event_create_step_done_rejected():
    """step_done was removed from valid event types."""
    with pytest.raises(ValidationError):
        WorkItemEventCreate(event_type="step_done")


def test_work_item_event_create_invalid_type():
    with pytest.raises(ValidationError):
        WorkItemEventCreate(event_type="invalid_event")


def test_work_item_event_read():
    now = datetime.now(UTC)
    read = WorkItemEventRead(
        id=1,
        work_item_id="wi-abc",
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

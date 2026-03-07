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
    schema = WorkItemCreate(title="Fix login bug")
    assert schema.title == "Fix login bug"
    assert schema.type == "feature"
    assert schema.priority == 0
    assert schema.max_iterations == 3
    assert schema.created_by == "system"
    assert schema.project_id is None


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


def test_work_item_create_invalid_type():
    with pytest.raises(ValidationError):
        WorkItemCreate(title="Test", type="invalid_type")


def test_work_item_read_from_attributes():
    now = datetime.now(UTC)

    class FakeModel:
        id = "wi-abc"
        project_id = "proj-1"
        type = "feature"
        title = "Test"
        description = None
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
    assert read.last_event is None
    assert read.elapsed_minutes is None


def test_work_item_update_partial():
    update = WorkItemUpdate(title="New title")
    data = update.model_dump(exclude_unset=True)
    assert data == {"title": "New title"}
    assert "description" not in data


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

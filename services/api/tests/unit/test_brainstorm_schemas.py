"""Unit tests for Brainstorm API schemas."""

from datetime import UTC, datetime
import uuid

from pydantic import ValidationError
import pytest

from src.schemas.brainstorm import (
    BrainstormCreate,
    BrainstormRead,
    BrainstormTransition,
    BrainstormUpdate,
)

PROJECT_UUID = str(uuid.UUID("00000000-0000-0000-0000-000000000001"))


def test_brainstorm_create_minimal():
    schema = BrainstormCreate(project_id=PROJECT_UUID, title="Worker isolation")
    assert schema.title == "Worker isolation"
    assert schema.created_by == "system"
    assert schema.content is None


def test_brainstorm_create_full():
    schema = BrainstormCreate(
        project_id=PROJECT_UUID,
        title="Worker isolation",
        content="# Analysis\n\nLong text...",
        created_by="claude",
    )
    assert schema.content == "# Analysis\n\nLong text..."
    assert schema.created_by == "claude"


def test_brainstorm_create_requires_project_id():
    with pytest.raises(ValidationError):
        BrainstormCreate(title="No project")


def test_brainstorm_create_requires_title():
    with pytest.raises(ValidationError):
        BrainstormCreate(project_id=PROJECT_UUID)


def test_brainstorm_read_from_attributes():
    now = datetime.now(UTC)

    class FakeModel:
        id = "bs-abc1"
        project_id = PROJECT_UUID
        title = "Test brainstorm"
        content = "Some content"
        status = "draft"
        created_by = "system"
        created_at = now
        updated_at = now

    read = BrainstormRead.model_validate(FakeModel(), from_attributes=True)
    assert read.id == "bs-abc1"
    assert read.status == "draft"
    assert read.content == "Some content"


def test_brainstorm_update_partial():
    update = BrainstormUpdate(title="New title")
    data = update.model_dump(exclude_unset=True)
    assert data == {"title": "New title"}
    assert "content" not in data


def test_brainstorm_update_content_only():
    update = BrainstormUpdate(content="Updated analysis")
    data = update.model_dump(exclude_unset=True)
    assert data == {"content": "Updated analysis"}


def test_brainstorm_transition_defaults():
    t = BrainstormTransition()
    assert t.reason is None
    assert t.actor == "system"


def test_brainstorm_transition_with_reason():
    t = BrainstormTransition(reason="Discussion complete", actor="claude")
    assert t.reason == "Discussion complete"
    assert t.actor == "claude"

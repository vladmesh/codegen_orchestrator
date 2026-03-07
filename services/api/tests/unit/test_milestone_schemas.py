"""Unit tests for Milestone API schemas."""

from pydantic import ValidationError
import pytest

from src.schemas.milestone import (
    MilestoneCreate,
    MilestoneRead,
    MilestoneTransition,
    MilestoneUpdate,
)


def test_milestone_create_minimal():
    ms = MilestoneCreate(project_id="proj-1", title="Phase 1")
    assert ms.project_id == "proj-1"
    assert ms.title == "Phase 1"
    assert ms.description is None
    assert ms.sort_order == 0
    assert ms.parent_id is None
    assert ms.created_by == "system"


def test_milestone_create_full():
    ms = MilestoneCreate(
        project_id="proj-1",
        title="Phase 2",
        description="Post-alpha stability",
        sort_order=1,
        parent_id="ms-0001",
        created_by="user",
    )
    assert ms.description == "Post-alpha stability"
    assert ms.sort_order == 1
    assert ms.parent_id == "ms-0001"
    assert ms.created_by == "user"


def test_milestone_create_requires_project_id():
    with pytest.raises(ValidationError):
        MilestoneCreate(title="Phase 1")


def test_milestone_create_requires_title():
    with pytest.raises(ValidationError):
        MilestoneCreate(project_id="proj-1")


def test_milestone_read_from_attributes():
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    ms = MilestoneRead(
        id="ms-0001",
        project_id="proj-1",
        title="Phase 1",
        description=None,
        sort_order=0,
        status="open",
        parent_id=None,
        created_by="system",
        created_at=now,
        updated_at=now,
    )
    assert ms.id == "ms-0001"
    assert ms.status == "open"


def test_milestone_update_partial():
    ms = MilestoneUpdate(title="New Title")
    assert ms.title == "New Title"
    assert ms.description is None
    assert ms.sort_order is None


def test_milestone_update_all_optional():
    ms = MilestoneUpdate()
    data = ms.model_dump(exclude_unset=True)
    assert data == {}


def test_milestone_transition_defaults():
    t = MilestoneTransition()
    assert t.actor == "system"
    assert t.reason is None


def test_milestone_transition_with_reason():
    t = MilestoneTransition(reason="all tasks done", actor="po")
    assert t.reason == "all tasks done"
    assert t.actor == "po"

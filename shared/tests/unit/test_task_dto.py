"""Unit tests for Task DTOs — TaskDTO, TaskCreate, TaskUpdate, TaskEventDTO, TaskEventCreate."""

from datetime import UTC, datetime
from typing import Any
import uuid

from shared.contracts.dto.task import (
    TaskCreate,
    TaskDTO,
    TaskEventCreate,
    TaskEventDTO,
    TaskEventType,
    TaskStatus,
    TaskType,
    TaskUpdate,
)

_NOW = datetime(2026, 3, 17, tzinfo=UTC)
_PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class TestTaskDTO:
    """TaskDTO should parse API response dicts."""

    SAMPLE_RESPONSE: dict[str, Any] = {
        "id": "task-abc123",
        "project_id": str(_PROJECT_ID),
        "type": "feature",
        "title": "Add login page",
        "description": "Full description",
        "plan": "## Step 1\nDo the thing",
        "status": "in_dev",
        "priority": 5,
        "acceptance_criteria": "Login works",
        "current_iteration": 1,
        "max_iterations": 3,
        "need_e2e": True,
        "created_by": "po",
        "source_brainstorm_id": "bs-111",
        "repository_id": "repo-222",
        "story_id": "story-333",
        "blocked_by_task_id": "task-blocker",
        "failure_metadata": {"error": "timeout"},
        "last_event": "iteration_start",
        "elapsed_minutes": 45.2,
        "created_at": _NOW.isoformat(),
        "updated_at": _NOW.isoformat(),
    }

    def test_parse_full_response(self):
        dto = TaskDTO.model_validate(self.SAMPLE_RESPONSE)
        assert dto.id == "task-abc123"
        assert dto.project_id == _PROJECT_ID
        assert dto.type == "feature"
        assert dto.title == "Add login page"
        assert dto.description == "Full description"
        assert dto.plan == "## Step 1\nDo the thing"
        assert dto.status == "in_dev"
        assert dto.priority == 5
        assert dto.current_iteration == 1
        assert dto.max_iterations == 3
        assert dto.need_e2e is True
        assert dto.created_by == "po"
        assert dto.source_brainstorm_id == "bs-111"
        assert dto.repository_id == "repo-222"
        assert dto.story_id == "story-333"
        assert dto.blocked_by_task_id == "task-blocker"
        assert dto.failure_metadata == {"error": "timeout"}
        assert dto.last_event == "iteration_start"
        assert dto.elapsed_minutes == 45.2

    def test_parse_minimal_response(self):
        minimal = {
            "id": "task-min",
            "project_id": str(_PROJECT_ID),
            "type": "fix",
            "title": "Fix bug",
            "description": None,
            "status": "backlog",
            "priority": 0,
            "acceptance_criteria": None,
            "current_iteration": 0,
            "max_iterations": 3,
            "need_e2e": False,
            "created_by": "system",
            "created_at": _NOW.isoformat(),
        }
        dto = TaskDTO.model_validate(minimal)
        assert dto.id == "task-min"
        assert dto.plan is None
        assert dto.source_brainstorm_id is None
        assert dto.last_event is None
        assert dto.elapsed_minutes is None
        assert dto.failure_metadata is None

    def test_model_dump_roundtrip(self):
        dto = TaskDTO.model_validate(self.SAMPLE_RESPONSE)
        data = dto.model_dump(mode="json")
        dto2 = TaskDTO.model_validate(data)
        assert dto2.id == dto.id
        assert dto2.project_id == dto.project_id


class TestTaskCreate:
    """TaskCreate should serialize for API requests."""

    def test_minimal(self):
        create = TaskCreate(project_id=_PROJECT_ID, title="New task")
        data = create.model_dump(mode="json")
        assert data["project_id"] == str(_PROJECT_ID)
        assert data["title"] == "New task"
        assert data["type"] == "feature"
        assert data["status"] == "backlog"
        assert data["priority"] == 0

    def test_full(self):
        create = TaskCreate(
            project_id=_PROJECT_ID,
            title="Complex task",
            type=TaskType.REFACTOR,
            status=TaskStatus.TODO,
            description="Details",
            acceptance_criteria="Tests pass",
            priority=10,
            max_iterations=5,
            need_e2e=True,
            created_by="architect",
            source_brainstorm_id="bs-1",
            repository_id="repo-1",
            story_id="story-1",
            blocked_by_task_id="task-dep",
        )
        data = create.model_dump(mode="json")
        assert data["type"] == "refactor"
        assert data["need_e2e"] is True
        assert data["blocked_by_task_id"] == "task-dep"


class TestTaskUpdate:
    """TaskUpdate should support partial updates."""

    def test_exclude_unset(self):
        update = TaskUpdate(title="New title", priority=5)
        data = update.model_dump(exclude_unset=True)
        assert data == {"title": "New title", "priority": 5}

    def test_all_fields_optional(self):
        update = TaskUpdate()
        data = update.model_dump(exclude_unset=True)
        assert data == {}


class TestTaskEventDTO:
    """TaskEventDTO should parse event API responses."""

    SAMPLE_EVENT: dict[str, Any] = {
        "id": 42,
        "task_id": "task-abc",
        "event_type": "status_change",
        "from_status": "backlog",
        "to_status": "todo",
        "iteration": None,
        "details": {"actor": "po"},
        "actor": "po",
        "created_at": _NOW.isoformat(),
        "updated_at": _NOW.isoformat(),
    }

    def test_parse_status_change(self):
        dto = TaskEventDTO.model_validate(self.SAMPLE_EVENT)
        assert dto.id == 42
        assert dto.task_id == "task-abc"
        assert dto.event_type == "status_change"
        assert dto.from_status == "backlog"
        assert dto.to_status == "todo"
        assert dto.actor == "po"

    def test_parse_iteration_event(self):
        event = {
            "id": 99,
            "task_id": "task-abc",
            "event_type": "iteration_start",
            "from_status": None,
            "to_status": None,
            "iteration": 2,
            "details": {"run_id": "eng-123"},
            "actor": "system",
            "created_at": _NOW.isoformat(),
        }
        dto = TaskEventDTO.model_validate(event)
        assert dto.iteration == 2
        assert dto.details == {"run_id": "eng-123"}


class TestTaskEventCreate:
    """TaskEventCreate should serialize for API requests."""

    def test_note_event(self):
        create = TaskEventCreate(
            event_type=TaskEventType.NOTE,
            details={"action": "step_start", "step": 1},
            actor="claude",
        )
        data = create.model_dump(mode="json")
        assert data["event_type"] == "note"
        assert data["actor"] == "claude"
        assert data["details"]["step"] == 1

    def test_defaults(self):
        create = TaskEventCreate(event_type=TaskEventType.STATUS_CHANGE)
        assert create.actor == "system"
        assert create.details == {}
        assert create.iteration is None

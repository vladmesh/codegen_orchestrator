"""Task DTOs and enums — single source of truth for task statuses and types (planning layer)."""

from enum import StrEnum
from typing import Any
import uuid

from pydantic import BaseModel

from shared.contracts.dto.base import TimestampedDTO


class TaskStatus(StrEnum):
    BACKLOG = "backlog"
    TODO = "todo"
    IN_DEV = "in_dev"
    IN_CI = "in_ci"
    TESTING = "testing"
    DONE = "done"
    BLOCKED = "blocked"
    WAITING_HUMAN_REVIEW = "waiting_human_review"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskType(StrEnum):
    CREATE = "create"
    FEATURE = "feature"
    FIX = "fix"
    REFACTOR = "refactor"


class TaskEventType(StrEnum):
    STATUS_CHANGE = "status_change"
    ITERATION_START = "iteration_start"
    ITERATION_END = "iteration_end"
    NOTE = "note"
    COMMENT = "comment"
    WORKER_REPORT = "worker_report"


# Valid status transitions: from_status -> set of allowed to_statuses
VALID_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.BACKLOG: {TaskStatus.TODO, TaskStatus.CANCELLED},
    TaskStatus.TODO: {TaskStatus.IN_DEV, TaskStatus.BACKLOG, TaskStatus.CANCELLED},
    TaskStatus.IN_DEV: {
        TaskStatus.IN_CI,
        TaskStatus.BLOCKED,
        TaskStatus.WAITING_HUMAN_REVIEW,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.IN_CI: {
        TaskStatus.IN_DEV,
        TaskStatus.TESTING,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.TESTING: {
        TaskStatus.DONE,
        TaskStatus.IN_DEV,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.BLOCKED: {TaskStatus.IN_DEV, TaskStatus.BACKLOG, TaskStatus.CANCELLED},
    TaskStatus.WAITING_HUMAN_REVIEW: {
        TaskStatus.IN_DEV,
        TaskStatus.BACKLOG,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.DONE: {TaskStatus.BACKLOG},
    TaskStatus.FAILED: {TaskStatus.BACKLOG, TaskStatus.CANCELLED},
    TaskStatus.CANCELLED: set(),
}


# --- Response DTOs ---


class TaskDTO(TimestampedDTO):
    """Task response from API."""

    id: str
    project_id: uuid.UUID
    type: TaskType
    title: str
    description: str | None = None
    plan: str | None = None
    status: TaskStatus
    priority: int
    acceptance_criteria: str | None = None
    current_iteration: int
    max_iterations: int
    need_e2e: bool = False
    created_by: str
    source_brainstorm_id: str | None = None
    repository_id: str | None = None
    story_id: str | None = None
    blocked_by_task_id: str | None = None
    failure_metadata: dict[str, Any] | None = None
    last_event: str | None = None
    elapsed_minutes: float | None = None


class TaskEventDTO(TimestampedDTO):
    """Task event response from API."""

    id: int
    task_id: str
    event_type: TaskEventType
    from_status: TaskStatus | None = None
    to_status: TaskStatus | None = None
    iteration: int | None = None
    details: dict[str, Any] = {}
    actor: str


# --- Request DTOs ---


class TaskCreate(BaseModel):
    """Create task request."""

    project_id: uuid.UUID
    type: TaskType = TaskType.FEATURE
    title: str
    status: TaskStatus = TaskStatus.BACKLOG
    description: str | None = None
    acceptance_criteria: str | None = None
    priority: int = 0
    max_iterations: int = 3
    need_e2e: bool = False
    created_by: str = "system"
    source_brainstorm_id: str | None = None
    repository_id: str | None = None
    story_id: str | None = None
    blocked_by_task_id: str | None = None


class TaskUpdate(BaseModel):
    """Update task request (non-status fields only)."""

    project_id: uuid.UUID | None = None
    title: str | None = None
    description: str | None = None
    plan: str | None = None
    acceptance_criteria: str | None = None
    priority: int | None = None
    need_e2e: bool | None = None
    repository_id: str | None = None
    story_id: str | None = None
    blocked_by_task_id: str | None = None
    source_brainstorm_id: str | None = None
    current_iteration: int | None = None
    failure_metadata: dict[str, Any] | None = None


class TaskEventCreate(BaseModel):
    """Create task event request."""

    event_type: TaskEventType
    iteration: int | None = None
    details: dict[str, Any] = {}
    actor: str = "system"

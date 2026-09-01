"""Task API schemas (planning layer)."""

from typing import Any
import uuid

from pydantic import BaseModel

from shared.contracts.dto.base import TimestampedDTO

# The request schemas are the contract every client already imports; the API
# validates against that same object rather than a look-alike of its own.
from shared.contracts.dto.task import TaskCreate, TaskEventCreate, TaskUpdate

__all__ = [
    "TaskCreate",
    "TaskEventCreate",
    "TaskEventRead",
    "TaskRead",
    "TaskResume",
    "TaskTransition",
    "TaskUpdate",
]


class TaskRead(TimestampedDTO):
    """Schema for reading a task."""

    id: str
    project_id: uuid.UUID
    type: str
    title: str
    description: str | None
    plan: str | None = None
    status: str
    priority: int
    acceptance_criteria: str | None
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
    dispatch_admitted: bool


class TaskTransition(BaseModel):
    """Schema for action endpoints (start, complete, fail, reopen, transition)."""

    reason: str | None = None
    actor: str = "system"
    details: dict[str, Any] = {}


class TaskResume(BaseModel):
    """Schema for resuming a task from WAITING_HUMAN_REVIEW."""

    guidance: str
    actor: str = "admin"


class TaskEventRead(TimestampedDTO):
    """Schema for reading a task event."""

    id: int
    task_id: str
    event_type: str
    from_status: str | None
    to_status: str | None
    iteration: int | None
    details: dict[str, Any]
    actor: str

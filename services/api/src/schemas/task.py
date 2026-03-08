"""Task API schemas (planning layer)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from shared.contracts.dto.task import TaskEventType, TaskType


class TaskCreate(BaseModel):
    """Schema for creating a task."""

    project_id: str
    type: TaskType = TaskType.FEATURE
    title: str
    description: str | None = None
    acceptance_criteria: str | None = None
    priority: int = 0
    max_iterations: int = 3
    need_e2e: bool = False
    created_by: str = "system"
    source_brainstorm_id: str | None = None
    milestone_id: str | None = None
    repository_id: str | None = None
    story_id: str | None = None


class TaskRead(BaseModel):
    """Schema for reading a task."""

    id: str
    project_id: str
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
    milestone_id: str | None = None
    repository_id: str | None = None
    story_id: str | None = None
    created_at: datetime
    updated_at: datetime
    last_event: str | None = None
    elapsed_minutes: float | None = None

    model_config = ConfigDict(from_attributes=True)


class TaskUpdate(BaseModel):
    """Schema for updating a task (non-status fields only)."""

    project_id: str | None = None
    title: str | None = None
    description: str | None = None
    plan: str | None = None
    acceptance_criteria: str | None = None
    priority: int | None = None
    need_e2e: bool | None = None
    milestone_id: str | None = None
    repository_id: str | None = None
    story_id: str | None = None


class TaskTransition(BaseModel):
    """Schema for action endpoints (start, complete, fail, reopen, transition)."""

    reason: str | None = None
    actor: str = "system"
    details: dict[str, Any] = {}


class TaskEventCreate(BaseModel):
    """Schema for creating a task event (iteration_start, iteration_end, note)."""

    event_type: TaskEventType
    iteration: int | None = None
    details: dict[str, Any] = {}
    actor: str = "system"


class TaskEventRead(BaseModel):
    """Schema for reading a task event."""

    id: int
    task_id: str
    event_type: str
    from_status: str | None
    to_status: str | None
    iteration: int | None
    details: dict[str, Any]
    actor: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

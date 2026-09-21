"""Task API schemas (planning layer)."""

from typing import Any
import uuid

from pydantic import BaseModel, Field

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
    """Schema for reading a task.

    One half of the Task response contract; the shared `TaskDTO` every client
    parses with is the other. `services/api/tests/unit/test_task_schemas.py`
    holds the two to the same field spec — name, annotation, requiredness and
    default — so a field cannot go missing, change type or become optional on
    one side alone. Keep any change here paired with `TaskDTO`.
    """

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
    # Paired with `TaskDTO`, field for field — see the class docstring.
    dispatch_admitted: bool
    planning_attempt_id: str | None = None
    last_event: str | None = None
    elapsed_minutes: float | None = None


class TaskTransition(BaseModel):
    """Schema for action endpoints (start, complete, fail, reopen, transition)."""

    reason: str | None = None
    actor: str = "system"
    details: dict[str, Any] = {}


#: The retries a resumed task gets after its fresh attempt, unless the operator
#: names another number: the allowance a new task starts with.
RESUME_RETRY_ALLOWANCE = 3


class TaskResume(BaseModel):
    """The operator's one fresh engineering attempt for a parked task.

    `retries` is the budget the new attempt gets on purpose: the task's
    `max_iterations` becomes the fresh attempt's iteration plus this number, so
    a failure of the new attempt is retried that many times before it parks again.
    """

    guidance: str
    actor: str = "admin"
    retries: int = Field(default=RESUME_RETRY_ALLOWANCE, ge=0, le=10)


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

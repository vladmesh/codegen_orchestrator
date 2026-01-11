from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskType(str, Enum):
    ENGINEERING = "engineering"
    DEPLOY = "deploy"


class TaskCreate(BaseModel):
    """Create task request."""

    project_id: str
    type: TaskType
    spec: str | None = None


class TaskDTO(BaseModel):
    """Task response."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    type: TaskType
    status: TaskStatus
    spec: str | None = None
    result: dict | None = None
    created_at: datetime
    updated_at: datetime | None = None

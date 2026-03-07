from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunType(StrEnum):
    ENGINEERING = "engineering"
    DEPLOY = "deploy"


class RunCreate(BaseModel):
    """Create run request."""

    project_id: str
    type: RunType
    spec: str | None = None


class RunDTO(BaseModel):
    """Run response."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    type: RunType
    status: RunStatus
    spec: str | None = None
    result: dict | None = None
    created_at: datetime
    updated_at: datetime | None = None

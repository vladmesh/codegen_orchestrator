from enum import StrEnum

from pydantic import BaseModel

from shared.contracts.dto.base import TimestampedDTO


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


class RunDTO(TimestampedDTO):
    """Run response."""

    id: str
    project_id: str
    type: RunType
    status: RunStatus
    story_id: str | None = None
    spec: str | None = None
    result: dict | None = None

"""Run schemas (execution layer)."""

from datetime import datetime
from typing import Any
import uuid

from pydantic import BaseModel

from shared.contracts.dto.base import TimestampedDTO

# The create schema is the contract; the API validates against that same object
# rather than a look-alike of its own.
from shared.contracts.dto.run import RunCreate, RunStatus, RunType, RunUpdate

__all__ = [
    "RunBase",
    "RunCreate",
    "RunRead",
    "RunUpdate",
]


class RunBase(BaseModel):
    """Base run schema."""

    id: str
    type: RunType
    status: RunStatus
    project_id: uuid.UUID | None = None
    user_id: int | None = None
    story_id: str | None = None
    task_id: str | None = None
    run_metadata: dict[str, Any] = {}
    result: dict[str, Any] | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    callback_stream: str | None = None
    iteration: int | None = None
    agent_profile: dict[str, Any] | None = None
    transcript_path: str | None = None
    transcript_truncated: bool | None = None


class RunRead(RunBase, TimestampedDTO):
    """Schema for reading a run."""

    # Read-only: written only by the story transition that routed this QA run.
    qa_routed_at: datetime | None = None

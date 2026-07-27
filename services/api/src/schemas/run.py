"""Run schemas (execution layer)."""

from datetime import datetime
from typing import Any
import uuid

from pydantic import BaseModel

from shared.contracts.dto.base import TimestampedDTO


class RunBase(BaseModel):
    """Base run schema."""

    id: str
    type: str
    status: str
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
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    agent_profile: dict[str, Any] | None = None
    transcript_path: str | None = None
    transcript_truncated: bool | None = None


class RunCreate(BaseModel):
    """Schema for creating a run."""

    id: str
    type: str
    project_id: uuid.UUID | None = None
    user_id: int | None = None
    story_id: str | None = None
    task_id: str | None = None
    run_metadata: dict[str, Any] = {}
    callback_stream: str | None = None


class RunRead(RunBase, TimestampedDTO):
    """Schema for reading a run."""


class RunUpdate(BaseModel):
    """Schema for updating a run."""

    status: str | None = None
    run_metadata: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error_message: str | None = None
    error_traceback: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    iteration: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    agent_profile: dict[str, Any] | None = None
    transcript_path: str | None = None
    transcript_truncated: bool | None = None

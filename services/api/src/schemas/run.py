"""Run schemas (execution layer)."""

from datetime import datetime
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict


class RunBase(BaseModel):
    """Base run schema."""

    id: str
    type: str
    status: str
    project_id: uuid.UUID | None = None
    user_id: int | None = None
    task_id: str | None = None
    run_metadata: dict[str, Any] = {}
    result: dict[str, Any] | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    callback_stream: str | None = None


class RunCreate(BaseModel):
    """Schema for creating a run."""

    id: str
    type: str
    project_id: uuid.UUID | None = None
    user_id: int | None = None
    run_metadata: dict[str, Any] = {}
    callback_stream: str | None = None


class RunRead(RunBase):
    """Schema for reading a run."""

    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RunUpdate(BaseModel):
    """Schema for updating a run."""

    status: str | None = None
    run_metadata: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error_message: str | None = None
    error_traceback: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

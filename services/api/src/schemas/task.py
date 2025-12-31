"""Task schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class TaskBase(BaseModel):
    """Base task schema."""

    id: str
    type: str
    status: str
    project_id: str | None = None
    user_id: int | None = None
    task_metadata: dict[str, Any] = {}
    result: dict[str, Any] | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    callback_stream: str | None = None


class TaskCreate(BaseModel):
    """Schema for creating a task."""

    id: str
    type: str
    project_id: str | None = None
    user_id: int | None = None
    task_metadata: dict[str, Any] = {}
    callback_stream: str | None = None


class TaskRead(TaskBase):
    """Schema for reading a task."""

    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TaskUpdate(BaseModel):
    """Schema for updating a task."""

    status: str | None = None
    result: dict[str, Any] | None = None
    error_message: str | None = None
    error_traceback: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

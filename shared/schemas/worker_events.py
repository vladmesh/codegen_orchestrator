"""Worker event schemas for CLI agent results."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter


class WorkerEvent(BaseModel):
    """Base event for worker lifecycle updates."""

    request_id: str
    event_type: Literal["started", "progress", "completed", "failed"]
    timestamp: datetime
    worker_type: Literal["droid", "claude_code", "codex"]


class WorkerStarted(WorkerEvent):
    """Worker started event."""

    event_type: Literal["started"] = "started"
    repo: str
    task_summary: str


class WorkerProgress(WorkerEvent):
    """Worker progress event."""

    event_type: Literal["progress"] = "progress"
    stage: str | None = None
    message: str
    progress_pct: int | None = None


class WorkerCompleted(WorkerEvent):
    """Worker completed event."""

    event_type: Literal["completed"] = "completed"
    commit_sha: str | None
    branch: str
    files_changed: list[str]
    summary: str


class WorkerFailed(WorkerEvent):
    """Worker failure event."""

    event_type: Literal["failed"] = "failed"
    error_type: str
    error_message: str
    logs_tail: str


WorkerEventUnion = Annotated[
    WorkerStarted | WorkerProgress | WorkerCompleted | WorkerFailed,
    Field(discriminator="event_type"),
]


def parse_worker_event(data: dict[str, Any]) -> WorkerEventUnion:
    """Parse a raw worker event payload into a typed schema."""

    return TypeAdapter(WorkerEventUnion).validate_python(data)

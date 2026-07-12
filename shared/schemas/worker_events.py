"""Worker event schemas for CLI agent results."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

from shared.contracts.vocab import LifecycleEvent, WorkerCliKind


class WorkerEvent(BaseModel):
    """Base event for worker lifecycle updates."""

    request_id: str
    event_type: LifecycleEvent
    timestamp: datetime
    worker_type: WorkerCliKind


class WorkerStarted(WorkerEvent):
    """Worker started event."""

    event_type: Literal[LifecycleEvent.STARTED] = LifecycleEvent.STARTED
    repo: str
    task_summary: str


class WorkerProgress(WorkerEvent):
    """Worker progress event."""

    event_type: Literal[LifecycleEvent.PROGRESS] = LifecycleEvent.PROGRESS
    stage: str | None = None
    message: str
    progress_pct: int | None = None


class WorkerCompleted(WorkerEvent):
    """Worker completed event."""

    event_type: Literal[LifecycleEvent.COMPLETED] = LifecycleEvent.COMPLETED
    commit_sha: str | None
    branch: str
    files_changed: list[str]
    summary: str


class WorkerFailed(WorkerEvent):
    """Worker failure event."""

    event_type: Literal[LifecycleEvent.FAILED] = LifecycleEvent.FAILED
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

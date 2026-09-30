"""`GET /api/stories/{id}/diagnostics` — why a story is where it is, for its owner's PO.

A story's owner used to learn about a stop only from its status, and a stop the
status did not show — a scaffold that failed under an ``in_progress`` story —
not at all. This read puts the causes the platform already has in one bounded,
redacted, read-only answer: the typed reason on the story, the project's
recorded ``scaffold_error``, the story's failed Runs, its tasks' moves into a
failure or a stop, and the newest error lines the log store holds about it.

Access is the project's: its owner, an administrator, or an internal service.
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.story_failure import (
    SCAFFOLD_ERROR_KEY,
    STORY_DIAGNOSTIC_LOG_HOURS_DEFAULT,
    STORY_DIAGNOSTIC_LOG_HOURS_MAX,
    STORY_DIAGNOSTIC_LOG_LIMIT,
    STORY_DIAGNOSTIC_RECORD_LIMIT,
    STORY_FAILURE_REASON,
    StoryDiagnosticRun,
    StoryDiagnosticsRead,
    StoryDiagnosticTaskEvent,
    StoryFailure,
    bounded_diagnostic,
)
from shared.contracts.dto.task import TaskEventType, TaskStatus
from shared.models import Project, Run, Task, TaskEvent

from ..database import get_async_session
from ..dependencies import _optional_bearer_scheme, is_internal_service
from ..story_diagnostics_logs import read_story_logs
from ._story_helpers import _get_story, work_cycle_task_count
from .projects_guards import check_project_access

logger = structlog.get_logger()

diagnostics_router = APIRouter()

#: Task statuses whose entry is a failure or a stop worth explaining.
_STOP_TASK_STATUSES = frozenset(
    {
        TaskStatus.FAILED.value,
        TaskStatus.BLOCKED.value,
        TaskStatus.WAITING_HUMAN_REVIEW.value,
        TaskStatus.WAITING_RESOURCES.value,
    }
)


def _typed_failure(quarantine_reason: dict | None) -> StoryFailure | None:
    if not isinstance(quarantine_reason, dict):
        return None
    if quarantine_reason.get("reason") != STORY_FAILURE_REASON:
        return None
    try:
        return StoryFailure.model_validate(quarantine_reason)
    except ValidationError:
        return None


def _serialized(value: object) -> str | None:
    if not value:
        return None
    return bounded_diagnostic(json.dumps(value, default=str, ensure_ascii=False))


async def _failed_runs(story_id: str, db: AsyncSession) -> list[StoryDiagnosticRun]:
    runs = await db.scalars(
        select(Run)
        .where(Run.story_id == story_id, Run.status == RunStatus.FAILED.value)
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(STORY_DIAGNOSTIC_RECORD_LIMIT)
    )
    return [
        StoryDiagnosticRun(
            id=run.id,
            type=run.type,
            status=run.status,
            error=None if run.error_message is None else bounded_diagnostic(run.error_message),
            completed_at=run.completed_at,
        )
        for run in runs
    ]


async def _task_failures(story_id: str, db: AsyncSession) -> list[StoryDiagnosticTaskEvent]:
    events = await db.scalars(
        select(TaskEvent)
        .join(Task, Task.id == TaskEvent.task_id)
        .where(
            Task.story_id == story_id,
            TaskEvent.event_type == TaskEventType.STATUS_CHANGE.value,
            TaskEvent.to_status.in_(_STOP_TASK_STATUSES),
        )
        .order_by(TaskEvent.created_at.desc(), TaskEvent.id.desc())
        .limit(STORY_DIAGNOSTIC_RECORD_LIMIT)
    )
    return [
        StoryDiagnosticTaskEvent(
            task_id=event.task_id,
            to_status=event.to_status,
            actor=event.actor,
            created_at=event.created_at,
            details=_serialized(event.details),
        )
        for event in events
    ]


@diagnostics_router.get("/{story_id}/diagnostics", response_model=StoryDiagnosticsRead)
async def get_story_diagnostics(  # noqa: PLR0913 — FastAPI dependencies
    story_id: str,
    log_hours: int = Query(
        STORY_DIAGNOSTIC_LOG_HOURS_DEFAULT, ge=1, le=STORY_DIAGNOSTIC_LOG_HOURS_MAX
    ),
    include_logs: bool = Query(True),
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> StoryDiagnosticsRead:
    """The recorded causes behind a story's state: bounded, redacted, read-only."""
    story = await _get_story(story_id, db)
    project = await db.get(Project, story.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    await check_project_access(
        project, x_telegram_id, db, is_internal=_is_internal, credentials=credentials
    )

    failure = _typed_failure(story.quarantine_reason)
    scaffold_error = (project.config or {}).get(SCAFFOLD_ERROR_KEY)
    logs, logs_unavailable = [], "not requested"
    if include_logs:
        logs, logs_unavailable = await read_story_logs(
            story.id,
            str(uuid.UUID(str(story.project_id))),
            hours=log_hours,
            limit=STORY_DIAGNOSTIC_LOG_LIMIT,
        )
    logger.info("story_diagnostics_read", story_id=story.id, logs=len(logs))
    return StoryDiagnosticsRead(
        story_id=story.id,
        project_id=str(story.project_id),
        story_status=story.status,
        project_status=project.status,
        failure=failure,
        quarantine_reason=None if failure else _serialized(story.quarantine_reason),
        scaffold_error=None if scaffold_error is None else bounded_diagnostic(scaffold_error),
        work_cycle_tasks=await work_cycle_task_count(story, db),
        failed_runs=await _failed_runs(story.id, db),
        task_failures=await _task_failures(story.id, db),
        logs=logs,
        logs_unavailable=logs_unavailable,
    )

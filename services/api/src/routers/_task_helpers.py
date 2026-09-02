"""Task router helpers — shared DB utilities, converters, validators."""

from datetime import UTC, datetime
import secrets

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.task import (
    VALID_TRANSITIONS,
    TaskEventType,
    TaskStatus,
)
from shared.models import Task, TaskEvent

from ..schemas.task import TaskRead


async def commit_or_raise_fk(db: AsyncSession) -> None:
    """Commit and convert FK violations into 422 responses."""
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Foreign key constraint violated: {exc.orig}",
        ) from exc


def generate_id() -> str:
    return f"task-{secrets.token_hex(4)}"


def to_read(task: Task, last_event: str | None = None) -> TaskRead:
    elapsed = None
    if task.created_at:
        elapsed = (datetime.now(UTC) - task.created_at.replace(tzinfo=UTC)).total_seconds() / 60
    return TaskRead(
        id=task.id,
        project_id=task.project_id,
        type=task.type,
        title=task.title,
        description=task.description,
        plan=task.plan,
        status=task.status,
        priority=task.priority,
        acceptance_criteria=task.acceptance_criteria,
        current_iteration=task.current_iteration,
        max_iterations=task.max_iterations,
        need_e2e=getattr(task, "need_e2e", False),
        created_by=task.created_by,
        source_brainstorm_id=getattr(task, "source_brainstorm_id", None),
        repository_id=getattr(task, "repository_id", None),
        story_id=getattr(task, "story_id", None),
        blocked_by_task_id=getattr(task, "blocked_by_task_id", None),
        failure_metadata=getattr(task, "failure_metadata", None),
        dispatch_admitted=task.dispatch_admitted,
        planning_attempt_id=task.planning_attempt_id,
        created_at=task.created_at,
        updated_at=task.updated_at,
        last_event=last_event,
        elapsed_minutes=round(elapsed, 1) if elapsed is not None else None,
    )


async def _load_task(task_id: str, db: AsyncSession, *, for_update: bool) -> Task:
    query = select(Task).where(Task.id == task_id)
    if for_update:
        query = query.with_for_update()
    result = await db.execute(query)
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )
    return task


async def get_task(task_id: str, db: AsyncSession) -> Task:
    """Read a task without taking a row lock — read-only paths only."""
    return await _load_task(task_id, db, for_update=False)


async def get_task_for_update(task_id: str, db: AsyncSession) -> Task:
    """Read a task with SELECT ... FOR UPDATE — every path that mutates the row.

    Concurrent transitions of the same task then serialize on the row, so the
    loser validates against the status the winner committed and is refused.
    """
    return await _load_task(task_id, db, for_update=True)


async def get_last_event_summary(task_id: str, db: AsyncSession) -> str | None:
    query = (
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id)
        .order_by(TaskEvent.created_at.desc())
        .limit(1)
    )
    result = await db.execute(query)
    event = result.scalar_one_or_none()
    if not event:
        return None
    if event.event_type == TaskEventType.STATUS_CHANGE:
        return f"{event.from_status} → {event.to_status}"
    return f"{event.event_type}: {event.details}" if event.details else event.event_type


async def create_status_event(
    task: Task,
    from_status: str,
    to_status: str,
    actor: str,
    details: dict,
    db: AsyncSession,
) -> TaskEvent:
    event = TaskEvent(
        task_id=task.id,
        event_type=TaskEventType.STATUS_CHANGE,
        from_status=from_status,
        to_status=to_status,
        actor=actor,
        details=details,
    )
    db.add(event)
    return event


def cancellation_is_reachable(from_status: str) -> bool:
    """Can a task in this status legally reach `cancelled` at all?

    The same `VALID_TRANSITIONS` question `validate_transition` asks, in the
    form a sweep over many rows needs: a caller that cancels a *set* of tasks
    has to leave the rows the transition table refuses where they are rather
    than force them or fail the whole transaction. A status the enum does not
    know is not cancellable either — it is not a status.
    """
    try:
        from_s = TaskStatus(from_status)
    except ValueError:
        return False
    return TaskStatus.CANCELLED in VALID_TRANSITIONS[from_s]


async def apply_cancellation(task: Task, db: AsyncSession) -> bool:
    """Move one *already locked* task row to cancelled, without committing.

    The whole of the cancel transition lives here — the already-cancelled no-op,
    the `VALID_TRANSITIONS` check, the status event — so that `DELETE
    /api/tasks/{id}` and the Product Brief takeover that voids a superseded plan
    are one writer with one set of rules, not two. Commit is the caller's,
    because a takeover cancels in the same transaction that mints the new
    planning attempt.

    Returns True when this call changed the row.
    """
    if task.status == TaskStatus.CANCELLED:
        return False
    validate_transition(task.status, TaskStatus.CANCELLED)
    old_status = task.status
    task.status = TaskStatus.CANCELLED
    await create_status_event(task, old_status, TaskStatus.CANCELLED, "system", {}, db)
    return True


def validate_transition(from_status: str, to_status: str) -> None:
    try:
        from_s = TaskStatus(from_status)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid status: {from_status}",
        ) from e
    try:
        to_s = TaskStatus(to_status)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid status: {to_status}",
        ) from e
    if to_s not in VALID_TRANSITIONS[from_s]:
        allowed = [s.value for s in VALID_TRANSITIONS[from_s]]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot transition from {from_status} to {to_status}. Allowed: {allowed}",
        )

"""Tasks router — CRUD + action-based status transitions + events (planning layer)."""

from datetime import UTC, datetime
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.task import (
    VALID_TRANSITIONS,
    TaskEventType,
    TaskStatus,
)
from shared.models import Task, TaskEvent

from ..database import get_async_session
from ..schemas.task import (
    TaskCreate,
    TaskEventCreate,
    TaskEventRead,
    TaskRead,
    TaskTransition,
    TaskUpdate,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _generate_id() -> str:
    return f"task-{secrets.token_hex(4)}"


def _to_read(task: Task, last_event: str | None = None) -> TaskRead:
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
        created_by=task.created_by,
        source_brainstorm_id=getattr(task, "source_brainstorm_id", None),
        milestone_id=getattr(task, "milestone_id", None),
        created_at=task.created_at,
        updated_at=task.updated_at,
        last_event=last_event,
        elapsed_minutes=round(elapsed, 1) if elapsed is not None else None,
    )


async def _get_task(task_id: str, db: AsyncSession) -> Task:
    query = select(Task).where(Task.id == task_id)
    result = await db.execute(query)
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )
    return task


async def _get_last_event_summary(task_id: str, db: AsyncSession) -> str | None:
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


async def _create_status_event(
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


def _validate_transition(from_status: str, to_status: str) -> None:
    try:
        from_s = TaskStatus(from_status)
    except ValueError:
        raise HTTPException(  # noqa: B904
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid status: {from_status}",
        )
    try:
        to_s = TaskStatus(to_status)
    except ValueError:
        raise HTTPException(  # noqa: B904
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid status: {to_status}",
        )
    if to_s not in VALID_TRANSITIONS[from_s]:
        allowed = [s.value for s in VALID_TRANSITIONS[from_s]]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Cannot transition from {from_status} to {to_status}. " f"Allowed: {allowed}",
        )


# --- CRUD ---


@router.post("/", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def create_task(
    body: TaskCreate,
    db: AsyncSession = Depends(get_async_session),
) -> TaskRead:
    now = datetime.now(UTC)
    task = Task(
        id=_generate_id(),
        project_id=body.project_id,
        type=body.type.value,
        title=body.title,
        description=body.description,
        status=TaskStatus.BACKLOG.value,
        priority=body.priority,
        acceptance_criteria=body.acceptance_criteria,
        current_iteration=0,
        max_iterations=body.max_iterations,
        created_by=body.created_by,
        source_brainstorm_id=body.source_brainstorm_id,
        milestone_id=body.milestone_id,
        created_at=now,
        updated_at=now,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    logger.info("task_created", task_id=task.id, title=task.title, type=task.type)
    return _to_read(task)


@router.get("/", response_model=list[TaskRead])
async def list_tasks(
    project_id: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    type_filter: str | None = Query(None, alias="type"),
    milestone_id: str | None = Query(None),
    since: datetime | None = Query(None),
    limit: int | None = Query(None, ge=1),
    sort: str | None = Query(None),
    db: AsyncSession = Depends(get_async_session),
) -> list[TaskRead]:
    query = select(Task)

    if project_id:
        query = query.where(Task.project_id == project_id)
    if status_filter:
        query = query.where(Task.status == status_filter)
    if type_filter:
        query = query.where(Task.type == type_filter)
    if milestone_id:
        query = query.where(Task.milestone_id == milestone_id)
    if since:
        query = query.where(Task.updated_at >= since)

    # Sorting
    if sort == "-created_at":
        query = query.order_by(Task.created_at.desc())
    elif sort == "created_at":
        query = query.order_by(Task.created_at.asc())
    else:
        query = query.order_by(Task.priority.asc(), Task.created_at.asc())

    if limit is not None:
        query = query.limit(limit)

    result = await db.execute(query)
    items = result.scalars().all()
    return [_to_read(task) for task in items]


@router.get("/stats")
async def get_task_stats(
    project_id: str | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> dict:
    """Return counts of tasks by status."""
    counts = {}
    total = 0
    for s in TaskStatus:
        query = select(func.count()).select_from(Task).where(Task.status == s.value)
        if project_id:
            query = query.where(Task.project_id == project_id)
        result = await db.execute(query)
        count = result.scalar_one()
        counts[s.value] = count
        total += count
    counts["total"] = total
    return counts


@router.get("/next-tag")
async def get_next_tag(
    db: AsyncSession = Depends(get_async_session),
) -> dict:
    """Return the next available backlog tag number (max existing tag + 1)."""
    # Titles follow pattern "#N Title..."
    query = select(Task.title).order_by(Task.created_at.desc())
    result = await db.execute(query)
    titles = result.scalars().all()

    max_tag = 0
    for title in titles:
        match = re.match(r"^#(\d+)\s", title)
        if match:
            tag = int(match.group(1))
            max_tag = max(tag, max_tag)

    return {"next_tag": max_tag + 1}


@router.get("/by-tag/{tag}", response_model=TaskRead)
async def get_task_by_tag(
    tag: str,
    db: AsyncSession = Depends(get_async_session),
) -> TaskRead:
    """Lookup task by backlog tag (e.g. '53' finds task titled '#53 ...')."""
    query = select(Task).where(Task.title.like(f"#{tag} %"))
    result = await db.execute(query)
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No task with tag #{tag}",
        )
    last_event = await _get_last_event_summary(task.id, db)
    return _to_read(task, last_event=last_event)


@router.get("/{task_id}", response_model=TaskRead)
async def get_task(
    task_id: str,
    db: AsyncSession = Depends(get_async_session),
) -> TaskRead:
    task = await _get_task(task_id, db)
    last_event = await _get_last_event_summary(task_id, db)
    return _to_read(task, last_event=last_event)


@router.patch("/{task_id}", response_model=TaskRead)
async def update_task(
    task_id: str,
    body: TaskUpdate,
    db: AsyncSession = Depends(get_async_session),
) -> TaskRead:
    task = await _get_task(task_id, db)

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(task, field, value)

    await db.commit()
    await db.refresh(task)

    logger.info("task_updated", task_id=task.id, fields=list(update_data.keys()))
    return _to_read(task)


@router.delete("/{task_id}", response_model=TaskRead)
async def cancel_task(
    task_id: str,
    db: AsyncSession = Depends(get_async_session),
) -> TaskRead:
    task = await _get_task(task_id, db)
    if task.status == TaskStatus.CANCELLED:
        return _to_read(task)

    _validate_transition(task.status, TaskStatus.CANCELLED)

    old_status = task.status
    task.status = TaskStatus.CANCELLED
    await _create_status_event(task, old_status, TaskStatus.CANCELLED, "system", {}, db)
    await db.commit()
    await db.refresh(task)

    logger.info("task_cancelled", task_id=task.id)
    return _to_read(task)


# --- Action endpoints (state machine transitions) ---


@router.post("/{task_id}/start", response_model=TaskRead)
async def start_task(
    task_id: str,
    body: TaskTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> TaskRead:
    body = body or TaskTransition()
    task = await _get_task(task_id, db)

    # Allow start from backlog (auto-promote to todo first) or from todo
    if task.status == TaskStatus.BACKLOG:
        await _create_status_event(task, TaskStatus.BACKLOG, TaskStatus.TODO, body.actor, {}, db)
        task.status = TaskStatus.TODO

    _validate_transition(task.status, TaskStatus.IN_DEV)

    old_status = task.status
    task.status = TaskStatus.IN_DEV
    await _create_status_event(task, old_status, TaskStatus.IN_DEV, body.actor, body.details, db)
    await db.commit()
    await db.refresh(task)

    logger.info("task_started", task_id=task.id)
    return _to_read(task)


@router.post("/{task_id}/complete", response_model=TaskRead)
async def complete_task(
    task_id: str,
    body: TaskTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> TaskRead:
    body = body or TaskTransition()
    task = await _get_task(task_id, db)

    _validate_transition(task.status, TaskStatus.DONE)

    old_status = task.status
    task.status = TaskStatus.DONE
    await _create_status_event(task, old_status, TaskStatus.DONE, body.actor, body.details, db)
    await db.commit()
    await db.refresh(task)

    logger.info("task_completed", task_id=task.id)
    return _to_read(task)


@router.post("/{task_id}/fail", response_model=TaskRead)
async def fail_task(
    task_id: str,
    body: TaskTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> TaskRead:
    body = body or TaskTransition()
    task = await _get_task(task_id, db)

    _validate_transition(task.status, TaskStatus.FAILED)

    old_status = task.status
    task.status = TaskStatus.FAILED
    details = body.details.copy()
    if body.reason:
        details["reason"] = body.reason
    await _create_status_event(task, old_status, TaskStatus.FAILED, body.actor, details, db)
    await db.commit()
    await db.refresh(task)

    logger.info("task_failed", task_id=task.id, reason=body.reason)
    return _to_read(task)


@router.post("/{task_id}/reopen", response_model=TaskRead)
async def reopen_task(
    task_id: str,
    body: TaskTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> TaskRead:
    body = body or TaskTransition()
    task = await _get_task(task_id, db)

    _validate_transition(task.status, TaskStatus.BACKLOG)

    old_status = task.status
    task.status = TaskStatus.BACKLOG
    details = body.details.copy()
    if body.reason:
        details["reason"] = body.reason
    await _create_status_event(task, old_status, TaskStatus.BACKLOG, body.actor, details, db)
    await db.commit()
    await db.refresh(task)

    logger.info("task_reopened", task_id=task.id, reason=body.reason)
    return _to_read(task)


@router.post("/{task_id}/transition", response_model=TaskRead)
async def transition_task(
    task_id: str,
    to_status: str = Query(...),
    body: TaskTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> TaskRead:
    body = body or TaskTransition()
    task = await _get_task(task_id, db)

    _validate_transition(task.status, to_status)

    old_status = task.status
    task.status = to_status
    await _create_status_event(task, old_status, to_status, body.actor, body.details, db)
    await db.commit()
    await db.refresh(task)

    logger.info("task_transitioned", task_id=task.id, from_s=old_status, to_s=to_status)
    return _to_read(task)


# --- Events ---


@router.get("/{task_id}/events", response_model=list[TaskEventRead])
async def list_task_events(
    task_id: str,
    event_type: str | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> list[TaskEventRead]:
    await _get_task(task_id, db)

    query = (
        select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.created_at.asc())
    )
    if event_type:
        query = query.where(TaskEvent.event_type == event_type)

    result = await db.execute(query)
    return list(result.scalars().all())


@router.post(
    "/{task_id}/events",
    response_model=TaskEventRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_task_event(
    task_id: str,
    body: TaskEventCreate,
    db: AsyncSession = Depends(get_async_session),
) -> TaskEvent:
    await _get_task(task_id, db)

    now = datetime.now(UTC)
    event = TaskEvent(
        task_id=task_id,
        event_type=body.event_type,
        iteration=body.iteration,
        details=body.details,
        actor=body.actor,
        created_at=now,
        updated_at=now,
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)

    logger.info(
        "task_event_created",
        task_id=task_id,
        event_type=event.event_type,
    )
    return event

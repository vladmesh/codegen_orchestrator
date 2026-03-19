"""Tasks router — CRUD + action-based status transitions + events (planning layer)."""

from datetime import UTC, datetime
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.task import TaskStatus
from shared.models import Task, TaskEvent

from ..database import get_async_session
from ..schemas.task import (
    TaskCreate,
    TaskEventCreate,
    TaskEventRead,
    TaskRead,
    TaskUpdate,
)
from ._task_actions import (
    _COMPLETE_PATH,
    action_router,
    complete_task,
    fail_task,
    reopen_task,
    resume_task,
    start_task,
    transition_task,
)
from ._task_helpers import (
    commit_or_raise_fk,
    create_status_event,
    generate_id,
    get_last_event_summary,
    get_task,
    to_read,
    validate_transition,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/tasks", tags=["tasks"])

# Include action endpoints (start, complete, fail, reopen, resume, transition)
router.include_router(action_router)

# Backward-compatible aliases (underscore-prefixed names used internally)
_commit_or_raise_fk = commit_or_raise_fk
_generate_id = generate_id
_to_read = to_read
_get_task = get_task
_get_last_event_summary = get_last_event_summary
_create_status_event = create_status_event
_validate_transition = validate_transition

__all__ = [
    "_COMPLETE_PATH",
    "_commit_or_raise_fk",
    "_create_status_event",
    "_generate_id",
    "_get_last_event_summary",
    "_get_task",
    "_to_read",
    "_validate_transition",
    "commit_or_raise_fk",
    "complete_task",
    "create_status_event",
    "fail_task",
    "generate_id",
    "get_last_event_summary",
    "get_task",
    "reopen_task",
    "resume_task",
    "router",
    "start_task",
    "to_read",
    "transition_task",
    "validate_transition",
]


class _TaskFilters:
    def __init__(
        self,
        project_id: uuid.UUID | None = None,
        status: str | None = Query(None),
        type: str | None = Query(None),
        source_brainstorm_id: str | None = Query(None),
        repository_id: str | None = Query(None),
        story_id: str | None = Query(None),
        since: datetime | None = Query(None),
        limit: int | None = Query(None, ge=1),
        sort: str | None = Query(None),
    ):
        self.project_id = project_id
        self.status = status
        self.type = type
        self.source_brainstorm_id = source_brainstorm_id
        self.repository_id = repository_id
        self.story_id = story_id
        self.since = since
        self.limit = limit
        self.sort = sort


# --- CRUD ---


@router.post("/", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def create_task(
    body: TaskCreate,
    db: AsyncSession = Depends(get_async_session),
) -> TaskRead:
    now = datetime.now(UTC)
    task = Task(
        id=generate_id(),
        project_id=body.project_id,
        type=body.type.value,
        title=body.title,
        description=body.description,
        status=body.status.value,
        priority=body.priority,
        acceptance_criteria=body.acceptance_criteria,
        current_iteration=0,
        max_iterations=body.max_iterations,
        need_e2e=body.need_e2e,
        created_by=body.created_by,
        source_brainstorm_id=body.source_brainstorm_id,
        repository_id=body.repository_id,
        story_id=body.story_id,
        blocked_by_task_id=body.blocked_by_task_id,
        created_at=now,
        updated_at=now,
    )
    db.add(task)
    await commit_or_raise_fk(db)
    await db.refresh(task)

    logger.info("task_created", task_id=task.id, title=task.title, type=task.type)
    return to_read(task)


@router.post("/push", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def push_task(
    body: TaskCreate,
    db: AsyncSession = Depends(get_async_session),
) -> TaskRead:
    """Create a task at the top of the backlog (lowest priority number)."""
    min_q = select(func.min(Task.priority)).where(Task.status == TaskStatus.BACKLOG)
    result = await db.execute(min_q)
    min_priority = result.scalar_one_or_none()
    auto_priority = (min_priority if min_priority is not None else 0) - 1

    now = datetime.now(UTC)
    task = Task(
        id=generate_id(),
        project_id=body.project_id,
        type=body.type.value,
        title=body.title,
        description=body.description,
        status=body.status.value,
        priority=auto_priority,
        acceptance_criteria=body.acceptance_criteria,
        current_iteration=0,
        max_iterations=body.max_iterations,
        need_e2e=body.need_e2e,
        created_by=body.created_by,
        source_brainstorm_id=body.source_brainstorm_id,
        repository_id=body.repository_id,
        story_id=body.story_id,
        blocked_by_task_id=body.blocked_by_task_id,
        created_at=now,
        updated_at=now,
    )
    db.add(task)
    await commit_or_raise_fk(db)
    await db.refresh(task)

    logger.info("task_pushed", task_id=task.id, title=task.title, priority=auto_priority)
    return to_read(task)


@router.get("/", response_model=list[TaskRead])
async def list_tasks(
    filters: _TaskFilters = Depends(),
    db: AsyncSession = Depends(get_async_session),
) -> list[TaskRead]:
    query = select(Task)

    if filters.project_id:
        query = query.where(Task.project_id == filters.project_id)
    if filters.status:
        query = query.where(Task.status == filters.status)
    if filters.type:
        query = query.where(Task.type == filters.type)
    if filters.source_brainstorm_id:
        query = query.where(Task.source_brainstorm_id == filters.source_brainstorm_id)
    if filters.repository_id:
        query = query.where(Task.repository_id == filters.repository_id)
    if filters.story_id:
        query = query.where(Task.story_id == filters.story_id)
    if filters.since:
        query = query.where(Task.updated_at >= filters.since)

    # Sorting
    if filters.sort == "-created_at":
        query = query.order_by(Task.created_at.desc())
    elif filters.sort == "created_at":
        query = query.order_by(Task.created_at.asc())
    else:
        query = query.order_by(Task.priority.asc(), Task.created_at.asc())

    if filters.limit is not None:
        query = query.limit(filters.limit)

    result = await db.execute(query)
    items = result.scalars().all()
    return [to_read(task) for task in items]


@router.get("/stats")
async def get_task_stats(
    project_id: uuid.UUID | None = None,
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
    last_event = await get_last_event_summary(task.id, db)
    return to_read(task, last_event=last_event)


@router.get("/{task_id}", response_model=TaskRead)
async def get_task_endpoint(
    task_id: str,
    db: AsyncSession = Depends(get_async_session),
) -> TaskRead:
    task = await get_task(task_id, db)
    last_event = await get_last_event_summary(task_id, db)
    return to_read(task, last_event=last_event)


@router.patch("/{task_id}", response_model=TaskRead)
async def update_task(
    task_id: str,
    body: TaskUpdate,
    db: AsyncSession = Depends(get_async_session),
) -> TaskRead:
    task = await get_task(task_id, db)

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(task, field, value)

    await commit_or_raise_fk(db)
    await db.refresh(task)

    logger.info("task_updated", task_id=task.id, fields=list(update_data.keys()))
    return to_read(task)


@router.delete("/{task_id}", response_model=TaskRead)
async def cancel_task(
    task_id: str,
    db: AsyncSession = Depends(get_async_session),
) -> TaskRead:
    task = await get_task(task_id, db)
    if task.status == TaskStatus.CANCELLED:
        return to_read(task)

    validate_transition(task.status, TaskStatus.CANCELLED)

    old_status = task.status
    task.status = TaskStatus.CANCELLED
    await create_status_event(task, old_status, TaskStatus.CANCELLED, "system", {}, db)
    await db.commit()
    await db.refresh(task)

    logger.info("task_cancelled", task_id=task.id)
    return to_read(task)


# --- Events ---


@router.get("/{task_id}/events", response_model=list[TaskEventRead])
async def list_task_events(
    task_id: str,
    event_type: str | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> list[TaskEventRead]:
    await get_task(task_id, db)

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
    await get_task(task_id, db)

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

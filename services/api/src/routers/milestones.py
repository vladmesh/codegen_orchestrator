"""Milestones router — CRUD + action-based status transitions."""

from datetime import UTC, datetime
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.milestone import VALID_TRANSITIONS, MilestoneStatus
from shared.models.milestone import Milestone
from shared.models.task import Task

from ..database import get_async_session
from ..schemas.milestone import (
    MilestoneCreate,
    MilestoneRead,
    MilestoneTransition,
    MilestoneUpdate,
)
from ..schemas.task import TaskRead

logger = structlog.get_logger()

router = APIRouter(prefix="/milestones", tags=["milestones"])


def _generate_id() -> str:
    return f"ms-{secrets.token_hex(4)}"


async def _get_milestone(milestone_id: str, db: AsyncSession) -> Milestone:
    query = select(Milestone).where(Milestone.id == milestone_id)
    result = await db.execute(query)
    ms = result.scalar_one_or_none()
    if not ms:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Milestone {milestone_id} not found",
        )
    return ms


def _validate_transition(from_status: str, to_status: str) -> None:
    try:
        from_s = MilestoneStatus(from_status)
    except ValueError:
        raise HTTPException(  # noqa: B904
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid status: {from_status}",
        )
    try:
        to_s = MilestoneStatus(to_status)
    except ValueError:
        raise HTTPException(  # noqa: B904
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid status: {to_status}",
        )
    if to_s not in VALID_TRANSITIONS[from_s]:
        allowed = [s.value for s in VALID_TRANSITIONS[from_s]]
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot transition from {from_status} to {to_status}. Allowed: {allowed}",
        )


def _to_read(ms: Milestone) -> MilestoneRead:
    return MilestoneRead.model_validate(ms, from_attributes=True)


def _task_to_read(task: Task) -> TaskRead:
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
        elapsed_minutes=round(elapsed, 1) if elapsed is not None else None,
    )


# --- CRUD ---


@router.post("/", response_model=MilestoneRead, status_code=status.HTTP_201_CREATED)
async def create_milestone(
    body: MilestoneCreate,
    db: AsyncSession = Depends(get_async_session),
) -> MilestoneRead:
    now = datetime.now(UTC)
    ms = Milestone(
        id=_generate_id(),
        project_id=body.project_id,
        title=body.title,
        description=body.description,
        sort_order=body.sort_order,
        status=MilestoneStatus.OPEN.value,
        parent_id=body.parent_id,
        created_by=body.created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(ms)
    await db.commit()
    await db.refresh(ms)

    logger.info("milestone_created", milestone_id=ms.id, title=ms.title)
    return _to_read(ms)


@router.get("/", response_model=list[MilestoneRead])
async def list_milestones(
    project_id: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    db: AsyncSession = Depends(get_async_session),
) -> list[MilestoneRead]:
    query = select(Milestone)

    if project_id:
        query = query.where(Milestone.project_id == project_id)
    if status_filter:
        query = query.where(Milestone.status == status_filter)

    query = query.order_by(Milestone.sort_order.asc())

    result = await db.execute(query)
    items = result.scalars().all()
    return [_to_read(ms) for ms in items]


@router.get("/{milestone_id}", response_model=MilestoneRead)
async def get_milestone(
    milestone_id: str,
    db: AsyncSession = Depends(get_async_session),
) -> MilestoneRead:
    ms = await _get_milestone(milestone_id, db)
    return _to_read(ms)


@router.patch("/{milestone_id}", response_model=MilestoneRead)
async def update_milestone(
    milestone_id: str,
    body: MilestoneUpdate,
    db: AsyncSession = Depends(get_async_session),
) -> MilestoneRead:
    ms = await _get_milestone(milestone_id, db)

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(ms, field, value)

    await db.commit()
    await db.refresh(ms)

    logger.info("milestone_updated", milestone_id=ms.id, fields=list(update_data.keys()))
    return _to_read(ms)


@router.delete("/{milestone_id}", response_model=MilestoneRead)
async def delete_milestone(
    milestone_id: str,
    db: AsyncSession = Depends(get_async_session),
) -> MilestoneRead:
    ms = await _get_milestone(milestone_id, db)
    await db.delete(ms)
    await db.commit()

    logger.info("milestone_deleted", milestone_id=ms.id)
    return _to_read(ms)


# --- Action endpoints ---


@router.post("/{milestone_id}/complete", response_model=MilestoneRead)
async def complete_milestone(
    milestone_id: str,
    body: MilestoneTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> MilestoneRead:
    ms = await _get_milestone(milestone_id, db)
    _validate_transition(ms.status, MilestoneStatus.COMPLETED.value)

    ms.status = MilestoneStatus.COMPLETED.value
    await db.commit()
    await db.refresh(ms)

    logger.info("milestone_completed", milestone_id=ms.id)
    return _to_read(ms)


# --- Sub-resources ---


@router.get("/{milestone_id}/tasks", response_model=list[TaskRead])
async def get_milestone_tasks(
    milestone_id: str,
    db: AsyncSession = Depends(get_async_session),
) -> list[TaskRead]:
    await _get_milestone(milestone_id, db)

    query = (
        select(Task)
        .where(Task.milestone_id == milestone_id)
        .order_by(Task.priority.asc(), Task.created_at.asc())
    )
    result = await db.execute(query)
    items = result.scalars().all()
    return [_task_to_read(task) for task in items]

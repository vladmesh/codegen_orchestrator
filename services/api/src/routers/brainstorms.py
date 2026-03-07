"""Brainstorms router — CRUD + action-based status transitions."""

from datetime import UTC, datetime
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.brainstorm import VALID_TRANSITIONS, BrainstormStatus
from shared.models.brainstorm import Brainstorm

from ..database import get_async_session
from ..schemas.brainstorm import (
    BrainstormCreate,
    BrainstormRead,
    BrainstormTransition,
    BrainstormUpdate,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/brainstorms", tags=["brainstorms"])


def _generate_id() -> str:
    return f"bs-{secrets.token_hex(4)}"


async def _get_brainstorm(brainstorm_id: str, db: AsyncSession) -> Brainstorm:
    query = select(Brainstorm).where(Brainstorm.id == brainstorm_id)
    result = await db.execute(query)
    bs = result.scalar_one_or_none()
    if not bs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Brainstorm {brainstorm_id} not found",
        )
    return bs


def _validate_transition(from_status: str, to_status: str) -> None:
    try:
        from_s = BrainstormStatus(from_status)
    except ValueError:
        raise HTTPException(  # noqa: B904
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid status: {from_status}",
        )
    try:
        to_s = BrainstormStatus(to_status)
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


async def _do_transition(
    brainstorm_id: str,
    to_status: BrainstormStatus,
    body: BrainstormTransition | None,
    db: AsyncSession,
) -> BrainstormRead:
    body = body or BrainstormTransition()
    bs = await _get_brainstorm(brainstorm_id, db)
    _validate_transition(bs.status, to_status.value)

    bs.status = to_status.value
    await db.commit()
    await db.refresh(bs)

    logger.info(
        "brainstorm_transitioned",
        brainstorm_id=bs.id,
        to_status=to_status.value,
        actor=body.actor,
    )
    return BrainstormRead.model_validate(bs, from_attributes=True)


# --- CRUD ---


@router.post("/", response_model=BrainstormRead, status_code=status.HTTP_201_CREATED)
async def create_brainstorm(
    body: BrainstormCreate,
    db: AsyncSession = Depends(get_async_session),
) -> BrainstormRead:
    now = datetime.now(UTC)
    bs = Brainstorm(
        id=_generate_id(),
        project_id=body.project_id,
        title=body.title,
        content=body.content,
        status=BrainstormStatus.DRAFT.value,
        created_by=body.created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(bs)
    await db.commit()
    await db.refresh(bs)

    logger.info("brainstorm_created", brainstorm_id=bs.id, title=bs.title)
    return BrainstormRead.model_validate(bs, from_attributes=True)


@router.get("/", response_model=list[BrainstormRead])
async def list_brainstorms(
    project_id: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    db: AsyncSession = Depends(get_async_session),
) -> list[BrainstormRead]:
    query = select(Brainstorm)

    if project_id:
        query = query.where(Brainstorm.project_id == project_id)
    if status_filter:
        query = query.where(Brainstorm.status == status_filter)

    query = query.order_by(Brainstorm.created_at.desc())

    result = await db.execute(query)
    items = result.scalars().all()
    return [BrainstormRead.model_validate(bs, from_attributes=True) for bs in items]


@router.get("/{brainstorm_id}", response_model=BrainstormRead)
async def get_brainstorm(
    brainstorm_id: str,
    db: AsyncSession = Depends(get_async_session),
) -> BrainstormRead:
    bs = await _get_brainstorm(brainstorm_id, db)
    return BrainstormRead.model_validate(bs, from_attributes=True)


@router.patch("/{brainstorm_id}", response_model=BrainstormRead)
async def update_brainstorm(
    brainstorm_id: str,
    body: BrainstormUpdate,
    db: AsyncSession = Depends(get_async_session),
) -> BrainstormRead:
    bs = await _get_brainstorm(brainstorm_id, db)

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(bs, field, value)

    await db.commit()
    await db.refresh(bs)

    logger.info("brainstorm_updated", brainstorm_id=bs.id, fields=list(update_data.keys()))
    return BrainstormRead.model_validate(bs, from_attributes=True)


@router.delete("/{brainstorm_id}", response_model=BrainstormRead)
async def delete_brainstorm(
    brainstorm_id: str,
    db: AsyncSession = Depends(get_async_session),
) -> BrainstormRead:
    bs = await _get_brainstorm(brainstorm_id, db)
    if bs.status == BrainstormStatus.ARCHIVED.value:
        return BrainstormRead.model_validate(bs, from_attributes=True)

    bs.status = BrainstormStatus.ARCHIVED.value
    await db.commit()
    await db.refresh(bs)

    logger.info("brainstorm_archived", brainstorm_id=bs.id)
    return BrainstormRead.model_validate(bs, from_attributes=True)


# --- Action endpoints ---


@router.post("/{brainstorm_id}/done", response_model=BrainstormRead)
async def mark_brainstorm_done(
    brainstorm_id: str,
    body: BrainstormTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> BrainstormRead:
    return await _do_transition(brainstorm_id, BrainstormStatus.DONE, body, db)


@router.post("/{brainstorm_id}/triage", response_model=BrainstormRead)
async def mark_brainstorm_triaged(
    brainstorm_id: str,
    body: BrainstormTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> BrainstormRead:
    return await _do_transition(brainstorm_id, BrainstormStatus.TRIAGED, body, db)


@router.post("/{brainstorm_id}/archive", response_model=BrainstormRead)
async def archive_brainstorm(
    brainstorm_id: str,
    body: BrainstormTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> BrainstormRead:
    return await _do_transition(brainstorm_id, BrainstormStatus.ARCHIVED, body, db)

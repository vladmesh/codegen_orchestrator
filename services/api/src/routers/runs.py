"""Runs router (execution layer)."""

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.models import Run, User

from ..database import get_async_session
from ..schemas import RunCreate, RunRead, RunUpdate

logger = structlog.get_logger()

router = APIRouter(prefix="/runs", tags=["runs"])


async def _resolve_user(
    telegram_id: int | None,
    db: AsyncSession,
) -> User | None:
    """Resolve User from telegram_id."""
    if not telegram_id:
        return None
    query = select(User).where(User.telegram_id == telegram_id)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def _check_run_access(
    run: Run,
    telegram_id: int | None,
    db: AsyncSession,
) -> None:
    """Check if user has access to run. Raises 403 if denied."""
    if telegram_id is None:
        return  # No auth header - allow (backward compat for internal calls)

    user = await _resolve_user(telegram_id, db)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with telegram_id {telegram_id} not found",
        )

    if user.is_admin:
        return

    # Regular user: must be owner
    if run.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: not run owner",
        )


@router.post("/", response_model=RunRead, status_code=status.HTTP_201_CREATED)
async def create_run(
    run: RunCreate,
    db: AsyncSession = Depends(get_async_session),
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
) -> Run:
    """Create a new run."""
    # Verify user exists if user_id provided
    if run.user_id:
        query = select(User).where(User.id == run.user_id)
        result = await db.execute(query)
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with id {run.user_id} not found",
            )

    db_run = Run(**run.model_dump())
    db.add(db_run)
    await db.commit()
    await db.refresh(db_run)

    logger.info(
        "run_created",
        run_id=db_run.id,
        run_type=db_run.type,
        user_id=db_run.user_id,
    )

    return db_run


@router.get("/{run_id}", response_model=RunRead)
async def get_run(
    run_id: str,
    db: AsyncSession = Depends(get_async_session),
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
) -> Run:
    """Get run by ID."""
    query = select(Run).where(Run.id == run_id)
    result = await db.execute(query)
    run = result.scalar_one_or_none()

    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )

    await _check_run_access(run, x_telegram_id, db)

    return run


@router.get("/", response_model=list[RunRead])
async def list_runs(
    project_id: str | None = None,
    run_type: str | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_async_session),
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
) -> list[Run]:
    """List runs with optional filters."""
    query = select(Run)

    # Apply filters
    if project_id:
        query = query.where(Run.project_id == project_id)
    if run_type:
        query = query.where(Run.type == run_type)
    if status:
        query = query.where(Run.status == status)

    # If user provided, filter by ownership
    if x_telegram_id:
        user = await _resolve_user(x_telegram_id, db)
        if user and not user.is_admin:
            query = query.where(Run.user_id == user.id)

    # Order by creation time (newest first)
    query = query.order_by(Run.created_at.desc())

    result = await db.execute(query)
    runs = result.scalars().all()

    return list(runs)


@router.patch("/{run_id}", response_model=RunRead)
async def update_run(
    run_id: str,
    run_update: RunUpdate,
    db: AsyncSession = Depends(get_async_session),
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
) -> Run:
    """Update run status and result."""
    query = select(Run).where(Run.id == run_id)
    result = await db.execute(query)
    run = result.scalar_one_or_none()

    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )

    # Only system (no telegram_id) or admins can update runs
    # Workers update run status, regular users cannot
    if x_telegram_id:
        user = await _resolve_user(x_telegram_id, db)
        if user and not user.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only system and admins can update runs",
            )

    # Update fields
    update_data = run_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if field == "run_metadata" and value is not None:
            # Merge metadata instead of replacing to preserve existing keys
            current = run.run_metadata or {}
            current.update(value)
            run.run_metadata = current
        else:
            setattr(run, field, value)

    await db.commit()
    await db.refresh(run)

    logger.info(
        "run_updated",
        run_id=run.id,
        status=run.status,
        updated_fields=list(update_data.keys()),
    )

    return run

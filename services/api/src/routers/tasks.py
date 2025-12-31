"""Tasks router."""

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.models import Task, User

from ..database import get_async_session
from ..schemas import TaskCreate, TaskRead, TaskUpdate

logger = structlog.get_logger()

router = APIRouter(prefix="/tasks", tags=["tasks"])


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


async def _check_task_access(
    task: Task,
    telegram_id: int | None,
    db: AsyncSession,
) -> None:
    """Check if user has access to task. Raises 403 if denied."""
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
    if task.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: not task owner",
        )


@router.post("/", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def create_task(
    task: TaskCreate,
    db: AsyncSession = Depends(get_async_session),
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
) -> Task:
    """Create a new task."""
    # Verify user exists if user_id provided
    if task.user_id:
        query = select(User).where(User.id == task.user_id)
        result = await db.execute(query)
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with id {task.user_id} not found",
            )

    db_task = Task(**task.model_dump())
    db.add(db_task)
    await db.commit()
    await db.refresh(db_task)

    logger.info(
        "task_created",
        task_id=db_task.id,
        task_type=db_task.type,
        user_id=db_task.user_id,
    )

    return db_task


@router.get("/{task_id}", response_model=TaskRead)
async def get_task(
    task_id: str,
    db: AsyncSession = Depends(get_async_session),
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
) -> Task:
    """Get task by ID."""
    query = select(Task).where(Task.id == task_id)
    result = await db.execute(query)
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )

    await _check_task_access(task, x_telegram_id, db)

    return task


@router.get("/", response_model=list[TaskRead])
async def list_tasks(
    project_id: str | None = None,
    task_type: str | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_async_session),
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
) -> list[Task]:
    """List tasks with optional filters."""
    query = select(Task)

    # Apply filters
    if project_id:
        query = query.where(Task.project_id == project_id)
    if task_type:
        query = query.where(Task.type == task_type)
    if status:
        query = query.where(Task.status == status)

    # If user provided, filter by ownership
    if x_telegram_id:
        user = await _resolve_user(x_telegram_id, db)
        if user and not user.is_admin:
            query = query.where(Task.user_id == user.id)

    # Order by creation time (newest first)
    query = query.order_by(Task.created_at.desc())

    result = await db.execute(query)
    tasks = result.scalars().all()

    return list(tasks)


@router.patch("/{task_id}", response_model=TaskRead)
async def update_task(
    task_id: str,
    task_update: TaskUpdate,
    db: AsyncSession = Depends(get_async_session),
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
) -> Task:
    """Update task status and result."""
    query = select(Task).where(Task.id == task_id)
    result = await db.execute(query)
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )

    # Only system (no telegram_id) or admins can update tasks
    # Workers update task status, regular users cannot
    if x_telegram_id:
        user = await _resolve_user(x_telegram_id, db)
        if user and not user.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only system and admins can update tasks",
            )

    # Update fields
    update_data = task_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(task, field, value)

    await db.commit()
    await db.refresh(task)

    logger.info(
        "task_updated",
        task_id=task.id,
        status=task.status,
        updated_fields=list(update_data.keys()),
    )

    return task

"""Users router."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import User

from ..database import get_async_session
from ..dependencies import is_internal_service
from ..schemas import UserCreate, UserRead, UserUpsert

router = APIRouter(prefix="/users", tags=["users"])


def _reject_admin_flag_from_outside(*, decides_admin: bool, is_internal: bool) -> None:
    """Only a service may decide the admin flag.

    Who is an administrator is settled by `ADMIN_TELEGRAM_IDS` and written by the
    bot, which reaches the API as an internal service. Anyone else sending
    `is_admin` over HTTP is either escalating themselves or demoting someone, so
    the request is refused rather than quietly stripped — a caller that silently
    got a non-admin user back would think it had worked.
    """
    if decides_admin and not is_internal:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="is_admin can only be set by an internal service",
        )


@router.post("/", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def create_user(
    user_in: UserCreate,
    is_internal: bool = Depends(is_internal_service),
    db: AsyncSession = Depends(get_async_session),
) -> User:
    """Create a new user."""
    # `is_admin` is a plain bool here, so "absent" and "false" look the same on
    # the wire: only an actual grant can be refused.
    _reject_admin_flag_from_outside(decides_admin=user_in.is_admin, is_internal=is_internal)

    # Check if user exists
    query = select(User).where(User.telegram_id == user_in.telegram_id)
    result = await db.execute(query)
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User with this telegram_id already exists",
        )

    user = User(
        telegram_id=user_in.telegram_id,
        username=user_in.username,
        first_name=user_in.first_name,
        last_name=user_in.last_name,
        is_admin=user_in.is_admin,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/upsert", response_model=UserRead)
async def upsert_user(
    user_in: UserUpsert,
    is_internal: bool = Depends(is_internal_service),
    db: AsyncSession = Depends(get_async_session),
) -> User:
    """Create or update user by telegram_id."""
    # Upsert can name the flag or leave it alone, and naming it at all is a
    # decision an outside caller does not get to make — in either direction.
    _reject_admin_flag_from_outside(
        decides_admin=user_in.is_admin is not None, is_internal=is_internal
    )

    query = select(User).where(User.telegram_id == user_in.telegram_id)
    result = await db.execute(query)
    user = result.scalar_one_or_none()

    if user:
        # Update existing user
        user.username = user_in.username
        user.first_name = user_in.first_name
        user.last_name = user_in.last_name
        if user_in.is_admin is not None:
            user.is_admin = user_in.is_admin
        user.last_seen = datetime.utcnow()
    else:
        # Create new user
        user = User(
            telegram_id=user_in.telegram_id,
            username=user_in.username,
            first_name=user_in.first_name,
            last_name=user_in.last_name,
            is_admin=user_in.is_admin if user_in.is_admin is not None else False,
        )
        db.add(user)

    await db.commit()
    await db.refresh(user)
    return user


@router.get("/", response_model=list[UserRead])
async def list_users(
    db: AsyncSession = Depends(get_async_session),
) -> list[User]:
    """List all users."""
    result = await db.execute(select(User))
    return result.scalars().all()


@router.get("/{user_id}", response_model=UserRead)
async def get_user(
    user_id: int,
    db: AsyncSession = Depends(get_async_session),
) -> User:
    """Get user by ID."""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.get("/by-telegram/{telegram_id}", response_model=UserRead)
async def get_user_by_telegram_id(
    telegram_id: int,
    db: AsyncSession = Depends(get_async_session),
) -> User:
    """Get user by Telegram ID."""
    query = select(User).where(User.telegram_id == telegram_id)
    result = await db.execute(query)
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user

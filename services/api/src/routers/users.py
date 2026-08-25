"""Users router."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.engineering_budget_policy import EngineeringBudgetPolicyState
from shared.models import EngineeringBudgetPolicy, PromoCode, User

from ..config import get_settings
from ..database import get_async_session
from ..dependencies import is_internal_service
from ..schemas import UserCreate, UserRead, UserUpsert

router = APIRouter(prefix="/users", tags=["users"])


def _is_owner_registration(telegram_id: int, internal_service: bool) -> bool:
    """Only the bot's internal owner registration bypasses promo redemption."""
    return internal_service and telegram_id in get_settings().get_admin_ids()


def _requires_promo(internal_service: bool, named_telegram_id: int | None) -> bool:
    """Only a named actor, rather than a service acting for itself, needs a code."""
    return not (internal_service and named_telegram_id is None)


def _new_user(user_in: UserCreate | UserUpsert, *, force_non_admin: bool = False) -> User:
    """Construct users in one place so admission never changes authorization."""
    is_admin = (
        False
        if force_non_admin
        else (bool(user_in.is_admin) or user_in.telegram_id in get_settings().get_admin_ids())
    )
    return User(
        telegram_id=user_in.telegram_id,
        username=user_in.username,
        first_name=user_in.first_name,
        last_name=user_in.last_name,
        is_admin=is_admin,
    )


def _promo_error(status_code: int, code: str) -> None:
    """Return a stable, machine-readable registration verdict."""
    raise HTTPException(status_code=status_code, detail={"code": code})


async def _redeem_promo_and_create_user(
    user_in: UserCreate | UserUpsert,
    db: AsyncSession,
) -> User:
    """Redeem a code, create its user and arm the existing policy in one transaction."""
    if not user_in.promo_code:
        _promo_error(status.HTTP_403_FORBIDDEN, "promo_code_required")
    normalized = user_in.promo_code.strip().upper()
    promo = await db.scalar(
        select(PromoCode).where(func.upper(PromoCode.code) == normalized).with_for_update()
    )
    if promo is None:
        _promo_error(status.HTTP_404_NOT_FOUND, "promo_code_not_found")
    if promo.redeemed_by_user_id is not None:
        _promo_error(status.HTTP_409_CONFLICT, "promo_code_redeemed")
    user = _new_user(user_in, force_non_admin=True)
    db.add(user)
    await db.flush()
    existing_policy = await db.get(EngineeringBudgetPolicy, user.id)
    if existing_policy is not None:
        _promo_error(status.HTTP_409_CONFLICT, "user_already_has_policy")
    db.add(
        EngineeringBudgetPolicy(
            user_id=user.id,
            limit_microusd=promo.credits_microusd,
            attempt_reservation_microusd=promo.attempt_reservation_microusd,
            state=EngineeringBudgetPolicyState.ENABLED,
            version=1,
        )
    )
    promo.redeemed_by_user_id = user.id
    promo.redeemed_at = datetime.now(UTC)
    return user


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
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
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

    if _is_owner_registration(user_in.telegram_id, is_internal) or not _requires_promo(
        is_internal, x_telegram_id
    ):
        user = _new_user(user_in)
        db.add(user)
    else:
        try:
            user = await _redeem_promo_and_create_user(user_in, db)
        except HTTPException:
            await db.rollback()
            raise
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/upsert", response_model=UserRead)
async def upsert_user(
    user_in: UserUpsert,
    is_internal: bool = Depends(is_internal_service),
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
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
        if user_in.promo_code and await db.get(EngineeringBudgetPolicy, user.id) is not None:
            _promo_error(status.HTTP_409_CONFLICT, "user_already_has_policy")
        # Update existing user
        user.username = user_in.username
        user.first_name = user_in.first_name
        user.last_name = user_in.last_name
        if user_in.is_admin is not None:
            user.is_admin = user_in.is_admin
        user.last_seen = datetime.utcnow()
    elif _is_owner_registration(user_in.telegram_id, is_internal) or not _requires_promo(
        is_internal, x_telegram_id
    ):
        user = _new_user(user_in)
        db.add(user)
    else:
        try:
            user = await _redeem_promo_and_create_user(user_in, db)
        except HTTPException:
            await db.rollback()
            raise

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

"""Internal/admin management of one-time promo codes."""

import secrets

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import PromoCode

from ..database import get_async_session
from ..dependencies import require_internal_or_admin
from ..schemas.promo_code import PromoCodeBatchCreate, PromoCodeRead

router = APIRouter(prefix="/promo-codes", tags=["promo-codes"])


def _new_code() -> str:
    """Generate a human-safe, case-insensitive code."""
    return secrets.token_urlsafe(18).upper()


@router.post("/batch", response_model=list[PromoCodeRead], status_code=status.HTTP_201_CREATED)
async def create_promo_code_batch(
    command: PromoCodeBatchCreate,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> list[PromoCode]:
    """Mint codes with the budget values that activation will arm."""
    codes = [
        PromoCode(
            code=_new_code(),
            credits_microusd=command.credits_microusd,
            attempt_reservation_microusd=command.attempt_reservation_microusd,
        )
        for _ in range(command.quantity)
    ]
    db.add_all(codes)
    await db.commit()
    for code in codes:
        await db.refresh(code)
    return codes


@router.get("", response_model=list[PromoCodeRead])
async def list_promo_codes(
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> list[PromoCode]:
    """List redemption state without exposing it to ordinary users."""
    return list((await db.scalars(select(PromoCode).order_by(PromoCode.id))).all())

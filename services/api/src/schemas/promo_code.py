"""Promo-code API schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PromoCodeBatchCreate(BaseModel):
    """Internal/admin request to mint a batch of one-time codes."""

    quantity: int = Field(gt=0, le=10_000)
    credits_microusd: int = Field(ge=0)
    attempt_reservation_microusd: int = Field(gt=0)


class PromoCodeRead(BaseModel):
    """The complete state of a code, visible only to administrators."""

    id: int
    code: str
    credits_microusd: int
    attempt_reservation_microusd: int
    redeemed_by_user_id: int | None
    redeemed_at: datetime | None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

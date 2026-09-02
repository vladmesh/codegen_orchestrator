"""User schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from shared.contracts.dto.base import TimestampedDTO


class UserBase(BaseModel):
    """Base user schema."""

    telegram_id: int = Field(description="Telegram user ID")
    username: str | None = Field(None, description="Telegram username")
    first_name: str | None = Field(None, description="First name")
    last_name: str | None = Field(None, description="Last name")


class UserCreate(UserBase):
    """Schema for creating a user."""

    is_admin: bool = False
    promo_code: str | None = Field(None, min_length=1, max_length=255)


class UserUpsert(UserBase):
    """Schema for upserting a user."""

    is_admin: bool | None = None
    promo_code: str | None = Field(None, min_length=1, max_length=255)


class UserRead(UserBase, TimestampedDTO):
    """Schema for reading a user."""

    id: int
    is_admin: bool
    last_seen: datetime
    model_config = ConfigDict(from_attributes=True)

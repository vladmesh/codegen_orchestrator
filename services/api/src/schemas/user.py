"""User schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class UserBase(BaseModel):
    """Base user schema."""

    telegram_id: int = Field(description="Telegram user ID")
    username: str | None = Field(None, description="Telegram username")
    first_name: str | None = Field(None, description="First name")
    last_name: str | None = Field(None, description="Last name")


class UserCreate(UserBase):
    """Schema for creating a user."""

    is_admin: bool = False


class UserUpdate(BaseModel):
    """Schema for updating a user."""
    
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    is_admin: bool | None = None


class UserRead(UserBase):
    """Schema for reading a user."""

    id: int
    is_admin: bool
    created_at: datetime
    last_seen: datetime
    model_config = ConfigDict(from_attributes=True)

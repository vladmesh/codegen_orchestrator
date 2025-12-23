"""User schemas."""

from pydantic import BaseModel, ConfigDict


class UserBase(BaseModel):
    """Base user schema."""

    telegram_id: int


class UserCreate(UserBase):
    """Schema for creating a user."""

    pass


class UserRead(UserBase):
    """Schema for reading a user."""

    id: int
    model_config = ConfigDict(from_attributes=True)

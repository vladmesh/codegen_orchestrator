"""Pydantic schemas for system configuration."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from shared.contracts.dto.base import TimestampedDTO


class SystemConfigCreate(BaseModel):
    """Schema for creating a system config."""

    key: str = Field(..., min_length=1, max_length=255, description="Dot-separated config key")
    value: Any = Field(..., description="Config value (JSON-serializable)")
    description: str | None = Field(default=None, description="Human-readable description")
    category: str = Field(..., min_length=1, max_length=50, description="Config category")
    updated_by: str | None = Field(default=None, description="Who created/updated this config")


class SystemConfigRead(TimestampedDTO):
    """Schema for reading a system config."""

    key: str
    value: Any
    description: str | None = None
    category: str
    updated_by: str | None = None

    model_config = ConfigDict(from_attributes=True)


class SystemConfigUpdate(BaseModel):
    """Schema for updating a system config (all fields optional)."""

    value: Any | None = None
    description: str | None = None
    category: str | None = None
    updated_by: str | None = None

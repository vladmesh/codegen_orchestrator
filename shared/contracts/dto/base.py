"""Base DTOs."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class BaseDTO(BaseModel):
    """Base DTO for all entities."""

    model_config = ConfigDict(from_attributes=True)


class TimestampedDTO(BaseDTO):
    """Base DTO with timestamps."""

    created_at: datetime
    updated_at: datetime | None = None

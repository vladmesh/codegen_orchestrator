"""Incident schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.contracts.dto.base import TimestampedDTO
from shared.contracts.dto.incident import IncidentStatus, IncidentType, require_server_handle


class IncidentBase(BaseModel):
    """Base incident schema."""

    server_handle: str | None = Field(
        default=None,
        description="Server handle; None only for provider_api_unavailable incidents",
    )
    incident_type: IncidentType = Field(description="Type of incident")
    details: dict = Field(default_factory=dict, description="Additional details")
    affected_services: list = Field(default_factory=list, description="List of affected services")


class IncidentCreate(IncidentBase):
    """Schema for creating an incident."""

    @model_validator(mode="after")
    def _require_server_handle(self) -> "IncidentCreate":
        require_server_handle(self.incident_type, self.server_handle)
        return self


class IncidentUpdate(BaseModel):
    """Schema for updating an incident."""

    status: IncidentStatus | None = None
    resolved_at: datetime | None = None
    details: dict | None = None
    recovery_attempts: int | None = None


class IncidentRead(IncidentBase, TimestampedDTO):
    """Schema for reading an incident."""

    id: int
    status: IncidentStatus
    detected_at: datetime
    resolved_at: datetime | None
    recovery_attempts: int
    model_config = ConfigDict(from_attributes=True)

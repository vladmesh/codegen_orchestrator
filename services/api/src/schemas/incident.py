"""Incident schemas."""

from datetime import datetime

from pydantic import ConfigDict, Field

from shared.contracts.dto.base import TimestampedDTO

# The request schemas are the contract every client already imports; the API
# validates against those same objects rather than look-alikes of its own.
from shared.contracts.dto.incident import (
    IncidentCreate,
    IncidentStatus,
    IncidentType,
    IncidentUpdate,
)

__all__ = [
    "IncidentCreate",
    "IncidentRead",
    "IncidentUpdate",
]


class IncidentRead(TimestampedDTO):
    """Schema for reading an incident."""

    server_handle: str | None = None
    incident_type: IncidentType
    details: dict = Field(default_factory=dict)
    affected_services: list[str] = Field(default_factory=list)
    id: int
    status: IncidentStatus
    detected_at: datetime
    resolved_at: datetime | None
    recovery_attempts: int
    model_config = ConfigDict(from_attributes=True)

"""Incident DTOs and enums — single source of truth for incident statuses and types."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from shared.contracts.dto.base import TimestampedDTO


class IncidentStatus(StrEnum):
    """Incident status lifecycle."""

    DETECTED = "detected"
    RECOVERING = "recovering"
    RESOLVED = "resolved"
    FAILED = "failed"


class IncidentType(StrEnum):
    """Types of incidents."""

    SERVER_UNREACHABLE = "server_unreachable"
    PROVISIONING_FAILED = "provisioning_failed"
    SERVICE_DOWN = "service_down"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    SSL_EXPIRING = "ssl_expiring"
    PROVIDER_API_UNAVAILABLE = "provider_api_unavailable"


# --- Response DTOs ---


class IncidentDTO(TimestampedDTO):
    """Incident response from API."""

    id: int
    server_handle: str | None = None
    incident_type: IncidentType
    status: IncidentStatus
    detected_at: datetime
    resolved_at: datetime | None = None
    details: dict = Field(default_factory=dict)
    affected_services: list[str] = Field(default_factory=list)
    recovery_attempts: int = 0


# --- Request DTOs ---


class IncidentCreate(BaseModel):
    """Create incident request."""

    # None for platform-level incidents that are not tied to a single server,
    # e.g. the provider API being unreachable.
    server_handle: str | None = None
    incident_type: IncidentType
    details: dict = Field(default_factory=dict)
    affected_services: list[str] = Field(default_factory=list)


class IncidentUpdate(BaseModel):
    """Update incident request."""

    status: IncidentStatus | None = None
    resolved_at: datetime | None = None
    details: dict | None = None
    recovery_attempts: int | None = None

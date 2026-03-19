"""Incident DTOs and enums — single source of truth for incident statuses and types."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

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


# --- Response DTOs ---


class IncidentDTO(TimestampedDTO):
    """Incident response from API."""

    id: int
    server_handle: str
    incident_type: str
    status: str
    detected_at: datetime
    resolved_at: datetime | None = None
    details: dict = {}
    affected_services: list = []
    recovery_attempts: int = 0


# --- Request DTOs ---


class IncidentCreate(BaseModel):
    """Create incident request."""

    server_handle: str
    incident_type: str
    details: dict = {}
    affected_services: list = []


class IncidentUpdate(BaseModel):
    """Update incident request."""

    status: str | None = None
    resolved_at: datetime | None = None
    details: dict | None = None
    recovery_attempts: int | None = None

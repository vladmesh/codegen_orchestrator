"""Incident DTOs and enums — single source of truth for incident statuses and types."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

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


# Incident types that are not tied to a single server. Everything else is
# server-bound: its active unique index is (server_handle, incident_type), and a
# NULL handle would silently break deduplication.
PLATFORM_LEVEL_INCIDENT_TYPES = frozenset({IncidentType.PROVIDER_API_UNAVAILABLE})


def require_server_handle(incident_type: IncidentType, server_handle: str | None) -> None:
    """Raise if a server-bound incident type comes without a handle."""
    if server_handle is None and incident_type not in PLATFORM_LEVEL_INCIDENT_TYPES:
        raise ValueError(f"server_handle is required for incident_type={incident_type.value}")


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

    # None only for platform-level incidents that are not tied to a single server,
    # i.e. PROVIDER_API_UNAVAILABLE. Every other type is server-bound.
    server_handle: str | None = None
    incident_type: IncidentType
    details: dict = Field(default_factory=dict)
    affected_services: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_server_handle(self) -> "IncidentCreate":
        require_server_handle(self.incident_type, self.server_handle)
        return self


class IncidentUpdate(BaseModel):
    """Update incident request."""

    status: IncidentStatus | None = None
    resolved_at: datetime | None = None
    details: dict | None = None
    recovery_attempts: int | None = None

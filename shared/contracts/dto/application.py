"""Application DTO — runtime state of a deployable unit on a server."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from shared.contracts.dto.base import TimestampedDTO


class ApplicationStatus(StrEnum):
    """Runtime state of an application on a server."""

    NOT_DEPLOYED = "not_deployed"
    DEPLOYING = "deploying"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    UNDEPLOYING = "undeploying"
    DOWN = "down"
    DEGRADED = "degraded"


# --- Response DTOs ---


class ApplicationDTO(TimestampedDTO):
    """Application response from API."""

    id: int
    repo_id: str
    server_handle: str
    service_name: str
    status: ApplicationStatus
    last_health_check: datetime | None = None
    response_time_ms: int | None = None
    ssl_expires_at: datetime | None = None
    uptime_pct_24h: float | None = None
    ports: list[dict[str, Any]] = []


# --- Request DTOs ---


class ApplicationCreate(BaseModel):
    """Create application request."""

    repo_id: str
    server_handle: str
    service_name: str
    status: ApplicationStatus = ApplicationStatus.NOT_DEPLOYED


class ApplicationUpdate(BaseModel):
    """Update application request."""

    status: ApplicationStatus | None = None
    last_health_check: datetime | None = None
    response_time_ms: int | None = None
    ssl_expires_at: datetime | None = None
    uptime_pct_24h: float | None = None

"""Application DTO — runtime state of a deployable unit on a server."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from shared.contracts.dto.base import TimestampedDTO

DEFAULT_APPLICATION_RESERVED_RAM_MB = 512


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


# The only statuses a health probe may write, and the only ones it may replace;
# every other status belongs to the deploy, stop and undeploy lifecycle.
PROBE_OWNED_STATUSES = frozenset(
    {ApplicationStatus.RUNNING, ApplicationStatus.DOWN, ApplicationStatus.DEGRADED}
)


# --- Response DTOs ---


class ApplicationDTO(TimestampedDTO):
    """Application response from API."""

    id: int
    repo_id: str
    server_handle: str
    service_name: str
    reserved_ram_mb: int = DEFAULT_APPLICATION_RESERVED_RAM_MB
    status: ApplicationStatus
    last_health_check: datetime | None = None
    response_time_ms: int | None = None
    ssl_expires_at: datetime | None = None
    uptime_pct_24h: float | None = None
    monitoring_enabled: bool = True
    monitoring_changed_at: datetime | None = None
    monitoring_changed_by: str | None = None
    ports: list[dict[str, Any]] = []


# --- Request DTOs ---


class ApplicationCreate(BaseModel):
    """Create application request."""

    repo_id: str
    server_handle: str
    service_name: str
    reserved_ram_mb: int = Field(default=DEFAULT_APPLICATION_RESERVED_RAM_MB, ge=1)
    status: ApplicationStatus = ApplicationStatus.NOT_DEPLOYED


class ApplicationMonitoringUpdate(BaseModel):
    """Administrative switch for health monitoring of one application.

    Not a status change: deployment status, port allocations and the bot binding
    stay as they are. Disabling is not a recovery either; an open SERVICE_DOWN
    incident of the application stays open and is marked muted.
    """

    enabled: bool
    reason: str | None = Field(default=None, max_length=500)


class ApplicationUpdate(BaseModel):
    """Update application request.

    A `probe` write carries a probe-owned status and is refused whole (409) when
    the row's current status is lifecycle-owned.
    """

    status: ApplicationStatus | None = None
    last_health_check: datetime | None = None
    response_time_ms: int | None = None
    ssl_expires_at: datetime | None = None
    uptime_pct_24h: float | None = None
    probe: bool = False

    @model_validator(mode="after")
    def _probe_writes_probe_owned_status(self) -> "ApplicationUpdate":
        if self.probe and self.status not in PROBE_OWNED_STATUSES:
            raise ValueError("a probe write must carry a status of running, down or degraded")
        return self

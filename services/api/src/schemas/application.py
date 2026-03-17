"""Application schemas."""

from datetime import datetime

from pydantic import BaseModel, Field

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.base import TimestampedDTO

from .port_allocation import PortAllocationRead


class ApplicationCreate(BaseModel):
    """Schema for creating an application."""

    repo_id: str
    server_handle: str
    service_name: str
    status: str = ApplicationStatus.NOT_DEPLOYED.value


class ApplicationRead(TimestampedDTO):
    """Schema for reading an application."""

    id: int
    repo_id: str
    server_handle: str
    service_name: str
    status: str
    last_health_check: datetime | None = None
    response_time_ms: int | None = None
    ssl_expires_at: datetime | None = None
    uptime_pct_24h: float | None = None
    ports: list[PortAllocationRead] = Field(default=[], validation_alias="port_allocations")


class ApplicationUpdate(BaseModel):
    """Schema for updating an application."""

    status: str | None = None
    last_health_check: datetime | None = None
    response_time_ms: int | None = None
    ssl_expires_at: datetime | None = None
    uptime_pct_24h: float | None = None


class ApplicationHealthHistoryCreate(BaseModel):
    """Schema for creating an application health history snapshot."""

    metrics: dict


class ApplicationHealthHistoryRead(TimestampedDTO):
    """Schema for reading an application health history entry."""

    id: int
    application_id: int
    recorded_at: datetime
    metrics: dict

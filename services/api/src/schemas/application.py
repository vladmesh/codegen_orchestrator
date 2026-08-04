"""Application schemas."""

from datetime import datetime

from pydantic import BaseModel, Field

# The request schemas are the contract every client already imports; the API
# validates against those same objects rather than look-alikes of its own.
from shared.contracts.dto.application import (
    ApplicationCreate,
    ApplicationUpdate,
)
from shared.contracts.dto.base import TimestampedDTO

from .port_allocation import PortAllocationRead

__all__ = [
    "ApplicationCreate",
    "ApplicationHealthHistoryCreate",
    "ApplicationHealthHistoryRead",
    "ApplicationRead",
    "ApplicationUpdate",
]


class ApplicationRead(TimestampedDTO):
    """Schema for reading an application."""

    id: int
    repo_id: str
    server_handle: str
    service_name: str
    reserved_ram_mb: int
    status: str
    last_health_check: datetime | None = None
    response_time_ms: int | None = None
    ssl_expires_at: datetime | None = None
    uptime_pct_24h: float | None = None
    ports: list[PortAllocationRead] = Field(default=[], validation_alias="port_allocations")


class ApplicationHealthHistoryCreate(BaseModel):
    """Schema for creating an application health history snapshot."""

    metrics: dict


class ApplicationHealthHistoryRead(TimestampedDTO):
    """Schema for reading an application health history entry."""

    id: int
    application_id: int
    recorded_at: datetime
    metrics: dict

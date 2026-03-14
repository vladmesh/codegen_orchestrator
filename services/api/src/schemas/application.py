"""Application schemas."""

from datetime import datetime

from pydantic import BaseModel

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.base import TimestampedDTO


class ApplicationCreate(BaseModel):
    """Schema for creating an application."""

    repo_id: str
    server_handle: str
    service_name: str
    port: int
    status: str = ApplicationStatus.NOT_DEPLOYED.value


class ApplicationRead(TimestampedDTO):
    """Schema for reading an application."""

    id: int
    repo_id: str
    server_handle: str
    service_name: str
    port: int
    status: str
    last_health_check: datetime | None = None


class ApplicationUpdate(BaseModel):
    """Schema for updating an application."""

    status: str | None = None
    port: int | None = None
    last_health_check: datetime | None = None

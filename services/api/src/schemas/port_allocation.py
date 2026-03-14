"""Port Allocation schemas."""

import uuid

from pydantic import BaseModel

from shared.contracts.dto.base import TimestampedDTO


class PortAllocationBase(BaseModel):
    """Base port allocation schema."""

    server_handle: str
    port: int
    service_name: str
    project_id: uuid.UUID | None = None


class PortAllocationCreate(PortAllocationBase):
    """Schema for creating a port allocation."""

    pass


class AllocateNextPortRequest(BaseModel):
    """Schema for atomic allocate-next-port request."""

    service_name: str
    project_id: uuid.UUID | None = None
    start_port: int = 8000


class PortAllocationRead(PortAllocationBase, TimestampedDTO):
    """Schema for reading a port allocation."""

    id: int

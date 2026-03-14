"""Port Allocation schemas."""

from pydantic import BaseModel

from shared.contracts.dto.base import TimestampedDTO


class PortAllocationBase(BaseModel):
    """Base port allocation schema."""

    server_handle: str
    port: int
    service_name: str
    application_id: int | None = None


class PortAllocationCreate(PortAllocationBase):
    """Schema for creating a port allocation."""

    pass


class AllocateNextPortRequest(BaseModel):
    """Schema for atomic allocate-next-port request."""

    service_name: str
    application_id: int | None = None
    start_port: int = 8000


class PortAllocationRead(PortAllocationBase, TimestampedDTO):
    """Schema for reading a port allocation."""

    id: int

"""Port Allocation schemas."""

from pydantic import BaseModel, ConfigDict
from typing import Optional


class PortAllocationBase(BaseModel):
    """Base port allocation schema."""

    server_handle: str
    port: int
    service_name: str
    project_id: Optional[str] = None


class PortAllocationCreate(PortAllocationBase):
    """Schema for creating a port allocation."""

    pass


class PortAllocationRead(PortAllocationBase):
    """Schema for reading a port allocation."""

    id: int
    model_config = ConfigDict(from_attributes=True)

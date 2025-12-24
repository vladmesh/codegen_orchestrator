"""Server schemas."""

from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, Any


class ServerBase(BaseModel):
    """Base server schema."""

    handle: str
    host: str
    public_ip: str
    ssh_user: str = "root"
    capacity_cpu: int = 1
    capacity_ram_mb: int = 1024
    labels: dict[str, Any] = {}
    is_managed: bool = True
    status: str = "active"
    notes: Optional[str] = None


class ServerCreate(ServerBase):
    """Schema for creating a server."""
    
    ssh_key: str = Field(description="Raw SSH private key to be encrypted")


class ServerRead(ServerBase):
    """Schema for reading a server."""
    
    # Exclude ssh_key from public read model
    model_config = ConfigDict(from_attributes=True)

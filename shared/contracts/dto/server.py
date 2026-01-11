from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict


class ServerStatus(str, Enum):
    NEW = "new"
    PENDING_SETUP = "pending_setup"
    ACTIVE = "active"
    UNREACHABLE = "unreachable"
    MAINTENANCE = "maintenance"


class ServerDTO(BaseModel):
    """Server response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    ip_address: str
    status: ServerStatus
    provider_id: str
    specs: dict = {}
    last_health_check: datetime | None = None

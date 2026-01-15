from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict


class ServerStatus(str, Enum):
    NEW = "new"
    PENDING_SETUP = "pending_setup"
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    UNREACHABLE = "unreachable"
    MAINTENANCE = "maintenance"
    FORCE_REBUILD = "force_rebuild"


class ServerCreate(BaseModel):
    """Create server request."""

    handle: str
    host: str
    public_ip: str
    provider_id: str
    is_managed: bool = True
    status: ServerStatus = ServerStatus.NEW
    labels: dict = {}


class ServerUpdate(BaseModel):
    """Update server request."""

    handle: str | None = None
    host: str | None = None
    public_ip: str | None = None
    status: ServerStatus | None = None
    labels: dict | None = None
    is_managed: bool | None = None
    provider_id: str | None = None
    capacity_cpu: int | None = None
    capacity_ram_mb: int | None = None
    capacity_disk_mb: int | None = None
    used_ram_mb: int | None = None
    used_disk_mb: int | None = None
    os_template: str | None = None
    provisioning_started_at: datetime | None = None


class ServerDTO(BaseModel):
    """Server response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    handle: str
    host: str
    public_ip: str
    status: ServerStatus
    provider_id: str
    is_managed: bool
    labels: dict = {}
    specs: dict = {}

    capacity_cpu: int = 0
    capacity_ram_mb: int = 0
    capacity_disk_mb: int = 0
    used_ram_mb: int = 0
    used_disk_mb: int = 0
    os_template: str | None = None

    last_health_check: datetime | None = None
    provisioning_started_at: datetime | None = None
    provisioning_attempts: int = 0

"""Server schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from shared.contracts.dto.base import BaseDTO, TimestampedDTO
from shared.contracts.dto.server import ServerStatus


class ServerBase(BaseModel):
    """Base server schema."""

    handle: str
    host: str
    public_ip: str
    ssh_user: str = "root"
    capacity_cpu: int = 1
    capacity_ram_mb: int = 1024
    capacity_disk_mb: int = 10240
    labels: dict[str, Any] = {}
    is_managed: bool = True
    status: str = ServerStatus.ACTIVE.value
    provider_id: str | None = None
    notes: str | None = None


class ServerCreate(ServerBase):
    """Schema for creating a server."""

    ssh_key: str | None = Field(None, description="Raw SSH private key to be encrypted")
    provider_id: str | None = None


class ServerRead(ServerBase, TimestampedDTO):
    """Schema for reading a server - includes usage and health metrics."""

    # Usage metrics (from provider API)
    used_ram_mb: int = 0
    used_disk_mb: int = 0
    os_template: str | None = None
    provisioning_started_at: datetime | None = None

    # Health metrics (from node_exporter + cadvisor)
    cpu_usage_pct: float | None = None
    load_avg_1m: float | None = None
    load_avg_5m: float | None = None
    load_avg_15m: float | None = None
    network_rx_errors: int | None = None
    network_tx_errors: int | None = None
    container_count_running: int | None = None
    container_count_total: int | None = None
    uptime_seconds: float | None = None
    last_health_check: datetime | None = None


class MetricsHistoryCreate(BaseModel):
    """Schema for creating a metrics history snapshot."""

    metrics: dict


class MetricsHistoryRead(BaseDTO):
    """Schema for reading a metrics history entry."""

    id: int
    server_handle: str
    recorded_at: datetime
    metrics: dict

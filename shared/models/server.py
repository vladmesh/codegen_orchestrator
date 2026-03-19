"""Server model."""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.contracts.dto.server import ServerStatus  # Single source of truth

from .base import Base


class Server(Base):
    """Server model - represents a VPS/Dedicated server."""

    __tablename__ = "servers"

    handle: Mapped[str] = mapped_column(String(255), primary_key=True)
    host: Mapped[str] = mapped_column(String(255))
    public_ip: Mapped[str] = mapped_column(String(255))
    ssh_user: Mapped[str] = mapped_column(String(50), default="root")
    # Store encrypted keys
    ssh_key_enc: Mapped[str | None] = mapped_column(String)

    # Capacity metrics (from Time4VPS API)
    capacity_cpu: Mapped[int] = mapped_column(Integer, default=1)
    capacity_ram_mb: Mapped[int] = mapped_column(Integer, default=1024)
    capacity_disk_mb: Mapped[int] = mapped_column(Integer, default=10240)  # 10GB default

    # Usage metrics (from Time4VPS API)
    used_ram_mb: Mapped[int] = mapped_column(Integer, default=0)
    used_disk_mb: Mapped[int] = mapped_column(Integer, default=0)

    # OS info
    os_template: Mapped[str | None] = mapped_column(String(100))

    # Management flags
    is_managed: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str] = mapped_column(String(50), default=ServerStatus.DISCOVERED.value)
    notes: Mapped[str | None] = mapped_column(String)

    # Health & Provisioning tracking
    last_health_check: Mapped[datetime | None] = mapped_column(DateTime)
    provisioning_attempts: Mapped[int] = mapped_column(Integer, default=0)
    provisioning_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_incident: Mapped[datetime | None] = mapped_column(DateTime)

    # Health metrics (from node_exporter + cadvisor, populated by health_checker)
    cpu_usage_pct: Mapped[float | None] = mapped_column(Float)
    load_avg_1m: Mapped[float | None] = mapped_column(Float)
    load_avg_5m: Mapped[float | None] = mapped_column(Float)
    load_avg_15m: Mapped[float | None] = mapped_column(Float)
    network_rx_errors: Mapped[int | None] = mapped_column(BigInteger)
    network_tx_errors: Mapped[int | None] = mapped_column(BigInteger)
    container_count_running: Mapped[int | None] = mapped_column(Integer)
    container_count_total: Mapped[int | None] = mapped_column(Integer)
    uptime_seconds: Mapped[float | None] = mapped_column(Float)

    labels: Mapped[dict] = mapped_column(JSON, default=dict)

    @property
    def available_ram_mb(self) -> int:
        """RAM available for new allocations."""
        return self.capacity_ram_mb - self.used_ram_mb

    @property
    def available_disk_mb(self) -> int:
        """Disk available for new allocations."""
        return self.capacity_disk_mb - self.used_disk_mb

    @property
    def provider_id(self) -> str | None:
        """Provider ID from labels."""
        return self.labels.get("provider_id")

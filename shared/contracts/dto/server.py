from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ServerStatus(StrEnum):
    """Server status lifecycle."""

    # Discovery
    DISCOVERED = "discovered"  # Обнаружен в Time4VPS API
    NEW = "new"  # Новый, ещё не классифицирован
    PENDING_SETUP = "pending_setup"  # Новый managed сервер, требует настройки

    # Provisioning
    PROVISIONING = "provisioning"  # Идет базовая настройка
    FORCE_REBUILD = "force_rebuild"  # 🔥 ТРИГГЕР: Полная переустановка

    # Operational
    READY = "ready"  # Настроен, готов принимать сервисы
    IN_USE = "in_use"  # Имеет активные сервисы
    ACTIVE = "active"  # Доступен и работает

    # Issues
    ERROR = "error"  # Инцидент: был в норме, доступ пропал
    UNREACHABLE = "unreachable"  # Недоступен по сети
    MAINTENANCE = "maintenance"  # Плановое обслуживание

    # Archive
    RESERVED = "reserved"  # Ghost server (личный)
    MISSING = "missing"  # Пропал из Time4VPS API
    DECOMMISSIONED = "decommissioned"  # Выведен из эксплуатации


class ServerCreate(BaseModel):
    """Create server request."""

    handle: str
    host: str
    public_ip: str
    is_managed: bool = True
    status: str = "discovered"  # Use str for flexibility
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

    handle: str
    host: str
    public_ip: str
    status: str  # Use str to accept any status value
    provider_id: str | None = None  # Computed from labels
    is_managed: bool
    labels: dict = {}

    capacity_cpu: int = 0
    capacity_ram_mb: int = 0
    capacity_disk_mb: int = 0
    used_ram_mb: int = 0
    used_disk_mb: int = 0
    os_template: str | None = None

    last_health_check: datetime | None = None
    provisioning_started_at: datetime | None = None
    provisioning_attempts: int = 0

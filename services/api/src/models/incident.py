"""Incident model for tracking server incidents."""

from datetime import datetime
from enum import Enum

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class IncidentStatus(str, Enum):
    """Incident status lifecycle."""

    DETECTED = "detected"  # Инцидент обнаружен
    RECOVERING = "recovering"  # Идет восстановление
    RESOLVED = "resolved"  # Успешно восстановлено
    FAILED = "failed"  # Восстановление не удалось


class IncidentType(str, Enum):
    """Types of incidents."""

    SERVER_UNREACHABLE = "server_unreachable"  # Сервер недоступен по SSH
    PROVISIONING_FAILED = "provisioning_failed"  # Ошибка настройки
    SERVICE_DOWN = "service_down"  # Сервис упал
    RESOURCE_EXHAUSTED = "resource_exhausted"  # Закончились ресурсы


class Incident(Base):
    """Incident model - tracks server and service incidents."""

    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    server_handle: Mapped[str] = mapped_column(
        String(255), ForeignKey("servers.handle"), index=True
    )
    incident_type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(50), default=IncidentStatus.DETECTED.value)

    detected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)

    details: Mapped[dict] = mapped_column(JSON, default=dict)
    affected_services: Mapped[list] = mapped_column(JSON, default=list)
    recovery_attempts: Mapped[int] = mapped_column(Integer, default=0)

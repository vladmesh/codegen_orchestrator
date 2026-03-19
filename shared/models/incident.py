"""Incident model for tracking server incidents."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.contracts.dto.incident import IncidentStatus, IncidentType  # noqa: F401

from .base import Base


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

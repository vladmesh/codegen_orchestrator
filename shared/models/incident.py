"""Incident model for tracking server incidents."""

from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from shared.contracts.dto.incident import IncidentStatus, IncidentType  # noqa: F401

from .base import Base


class Incident(Base):
    """Incident model - tracks server and service incidents."""

    __tablename__ = "incidents"
    __table_args__ = (
        CheckConstraint(
            "server_handle IS NOT NULL OR incident_type = "
            f"'{IncidentType.PROVIDER_API_UNAVAILABLE.value}'",
            name="ck_incidents_server_handle_required",
        ),
        Index(
            "uq_incidents_active_provisioning_failure",
            "server_handle",
            "incident_type",
            unique=True,
            postgresql_where=text(
                f"incident_type = '{IncidentType.PROVISIONING_FAILED.value}' "
                f"AND status IN ('{IncidentStatus.DETECTED.value}', "
                f"'{IncidentStatus.RECOVERING.value}')"
            ),
        ),
        Index(
            "uq_incidents_active_target_not_ready",
            "server_handle",
            "incident_type",
            unique=True,
            postgresql_where=text(
                f"incident_type = '{IncidentType.TARGET_NOT_READY.value}' "
                f"AND status IN ('{IncidentStatus.DETECTED.value}', "
                f"'{IncidentStatus.RECOVERING.value}')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # NULL only for a provider API outage: every other type is deduplicated by
    # (server_handle, incident_type), and NULLs never collide in that index.
    server_handle: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("servers.handle"), index=True, nullable=True
    )
    incident_type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(50), default=IncidentStatus.DETECTED.value)

    detected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)

    details: Mapped[dict] = mapped_column(JSON, default=dict)
    affected_services: Mapped[list] = mapped_column(JSON, default=list)
    recovery_attempts: Mapped[int] = mapped_column(Integer, default=0)

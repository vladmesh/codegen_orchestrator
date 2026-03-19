"""Deployment model — immutable log of deploy attempts."""

from datetime import datetime
from enum import StrEnum
import uuid

from sqlalchemy import JSON, ForeignKey, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from shared.contracts.dto.deployment import DeploymentResult

from .base import Base


class DeploymentStatus(StrEnum):
    """Legacy enum — kept for backward compatibility during migration.

    Use DeploymentResult from shared.contracts.dto.deployment instead.
    """

    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    PENDING = "pending"


class Deployment(Base):
    """Immutable record of a deployment attempt."""

    __tablename__ = "service_deployments"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Link to Application (nullable for backward compat with existing data)
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id"), nullable=True, index=True
    )

    # Denormalized fields — kept for queries and backward compat
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    service_name: Mapped[str]
    server_handle: Mapped[str] = mapped_column(
        ForeignKey("servers.handle", ondelete="CASCADE"), index=True
    )
    port: Mapped[int]

    # Deployment result — replaces old 'status' field
    result: Mapped[str] = mapped_column(default=DeploymentResult.PENDING.value, index=True)

    # Timestamps
    deployed_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    # Deployment metadata for redeployment
    deployment_info: Mapped[dict] = mapped_column(JSON, default=dict)

    # Git SHA of the deployed commit
    deployed_sha: Mapped[str | None] = mapped_column(nullable=True)

    def __repr__(self) -> str:
        return (
            f"<Deployment(id={self.id}, project={self.project_id}, "
            f"service={self.service_name}, server={self.server_handle}, "
            f"result={self.result})>"
        )

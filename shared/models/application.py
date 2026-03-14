"""Application model — runtime representation of a deployable unit on a server."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from shared.contracts.dto.application import ApplicationStatus

from .base import Base


class Application(Base):
    """A deployable unit running on a server. Links a repository to a server."""

    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("repo_id", "server_handle", name="uq_application_repo_server"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # What code (repository) and where (server)
    repo_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("repositories.id"), nullable=False, index=True
    )
    server_handle: Mapped[str] = mapped_column(
        ForeignKey("servers.handle", ondelete="CASCADE"), nullable=False, index=True
    )

    # Human-readable name (e.g. "fortune-teller-bot")
    service_name: Mapped[str] = mapped_column(String(255))

    # Network port on the server
    port: Mapped[int]

    # Runtime state — updated by deploy consumer and health checker
    status: Mapped[str] = mapped_column(
        String(50), default=ApplicationStatus.NOT_DEPLOYED.value, index=True
    )

    # Last health check timestamp — updated by health checker
    last_health_check: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    def __repr__(self) -> str:
        return (
            f"<Application(id={self.id}, service={self.service_name}, "
            f"server={self.server_handle}, status={self.status})>"
        )

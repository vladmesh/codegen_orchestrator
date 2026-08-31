"""Temporary access grant: durable record of access handed to a test identity."""

from datetime import datetime
import uuid

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from shared.contracts.dto.temporary_access import TemporaryAccessStatus

from .base import Base


class TemporaryAccessGrant(Base):
    """One durable QA identity grant bound to one deployed application."""

    __tablename__ = "temporary_access_grants"
    __table_args__ = (
        Index(
            "uq_temporary_access_grants_live_target",
            "project_id",
            "target_application_id",
            unique=True,
            postgresql_where=text(
                f"status != '{TemporaryAccessStatus.REVOKED.value}' "
                "AND target_application_id IS NOT NULL"
            ),
        ),
    )

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("projects.id"), index=True)

    # These legacy columns remain only so terminal slot history is readable.
    # New records never populate them; a live row without a target is rejected.
    legacy_env_key: Mapped[str | None] = mapped_column("env_key", String(255), nullable=True)
    legacy_subject: Mapped[str | None] = mapped_column("subject", String(255), nullable=True)

    channel: Mapped[str | None] = mapped_column(String(50), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_application_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_base_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    # Immutable deployed source identity, used to reject a newer target on retry.
    head_sha: Mapped[str] = mapped_column(String(64))

    # The QA run this grant exists for; its terminal state releases the grant.
    qa_run_id: Mapped[str] = mapped_column(String(255), ForeignKey("runs.id"), index=True)

    # The capability-operation runs and held QA handoff make restart idempotent.
    grant_run_id: Mapped[str] = mapped_column(String(255))
    grant_attempts: Mapped[int] = mapped_column(Integer, default=1)
    qa_message: Mapped[dict] = mapped_column(JSON)

    status: Mapped[str] = mapped_column(
        String(50), default=TemporaryAccessStatus.GRANTING.value, index=True
    )
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    qa_dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    revoke_run_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    revoke_attempts: Mapped[int] = mapped_column(Integer, default=0)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<TemporaryAccessGrant(id={self.id}, project={self.project_id}, "
            f"target={self.target_application_id}, status={self.status})>"
        )

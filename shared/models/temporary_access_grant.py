"""Temporary access grant: durable record of access handed to a test identity."""

from datetime import datetime
import uuid

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from shared.contracts.dto.temporary_access import TemporaryAccessStatus

from .base import Base


class TemporaryAccessGrant(Base):
    """One temporary environment value handed out for one QA run.

    The row is written before the deploy that applies the value, so the access
    is never held without a record of it. Only one live grant may exist per
    (project, env_key): the contract has a single slot for the value, and two
    live grants would overwrite each other's revoke.
    """

    __tablename__ = "temporary_access_grants"
    __table_args__ = (
        Index(
            "uq_temporary_access_grants_live_slot",
            "project_id",
            "env_key",
            unique=True,
            postgresql_where=text(f"status != '{TemporaryAccessStatus.REVOKED.value}'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("projects.id"), index=True)

    # Contract literal the value is written to, and the identity it names.
    env_key: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(255))

    # Commit the access was deployed on. The revoke redeploys this same commit.
    head_sha: Mapped[str] = mapped_column(String(64))

    # The QA run this grant exists for; its terminal state releases the grant.
    qa_run_id: Mapped[str] = mapped_column(String(255), ForeignKey("runs.id"), index=True)

    # The deploy run that applies the value, and the handoff held until it
    # confirms. Both are stored so a restart can finish what a dead process
    # started instead of leaving the access without a reader.
    grant_run_id: Mapped[str] = mapped_column(String(255))
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

    # What the running service was last seen holding. The grant is closed from
    # here and nowhere else: a deploy is a request, and only a reading of the
    # server says the value is gone.
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    slot_clear_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    slot_clear_readings: Mapped[int] = mapped_column(Integer, default=0)

    def __repr__(self) -> str:
        return (
            f"<TemporaryAccessGrant(id={self.id}, project={self.project_id}, "
            f"key={self.env_key}, status={self.status})>"
        )

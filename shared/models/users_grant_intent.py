"""Durable permanent-access intent, deliberately separate from deploy attempts."""

from datetime import datetime
import uuid

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class UsersGrantIntent(Base):
    """One idempotent permanent grant request and its current target binding.

    A deploy Run is an execution attempt, not this record.  Rebinding changes
    this record's target and creates another Run, retaining the old binding in
    ``target_history`` for audit.
    """

    __tablename__ = "users_grant_intents"
    __table_args__ = (
        UniqueConstraint(
            "kind", "project_id", "channel", "external_id", name="uq_users_grant_intent"
        ),
    )

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("projects.id"), index=True)
    channel: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str] = mapped_column(String(255))
    target_application_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_deployment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_sha: Mapped[str] = mapped_column(String(64))
    target_history: Mapped[list] = mapped_column(JSON, default=list)
    initiating_actor: Mapped[str] = mapped_column(String(255))
    outgoing_owner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    incoming_owner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_run_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("runs.id"), nullable=True
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

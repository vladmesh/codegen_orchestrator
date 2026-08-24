"""Append-only ledger for terminal engineering coding-agent attempts."""

from datetime import datetime
import uuid

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class EngineeringAttemptLedger(Base):
    """One canonical accounting record per engineering run delivery identity."""

    __tablename__ = "engineering_attempt_ledger"
    __table_args__ = (
        CheckConstraint("role = 'engineering'", name="ck_engineering_attempt_role"),
        CheckConstraint(
            "(cost_source = 'unknown' AND cost_microusd IS NULL) OR "
            "(cost_source = 'provider_reported' AND cost_microusd IS NOT NULL "
            "AND provider IS NOT NULL)",
            name="ck_engineering_attempt_cost_provenance",
        ),
        CheckConstraint(
            "owner_attribution IN ('resolved', 'unknown')",
            name="ck_engineering_attempt_owner_attribution",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    run_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("runs.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    story_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("stories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    task_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True
    )
    owner_attribution: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="engineering")
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_microusd: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cost_source: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")

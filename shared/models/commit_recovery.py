"""Agent-free claim and receipt, never an engineering accounting fact."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class CommitRecovery(Base):
    __tablename__ = "commit_recoveries"

    attempt_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    story_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("stories.id", ondelete="CASCADE"), nullable=False
    )
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    stop_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # API-derived identity, immutable after claim. No caller paths or credentials.
    identity: Mapped[dict] = mapped_column(JSON, nullable=False)
    receipt: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    handed_off_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

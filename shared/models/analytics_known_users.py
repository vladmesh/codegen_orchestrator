"""AnalyticsKnownUsers model — registry of seen user_id hashes per project."""

from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, PrimaryKeyConstraint, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base


class AnalyticsKnownUsers(Base):
    """Tracks first/last seen per (project, user_id_hash) for new/returning users."""

    __tablename__ = "analytics_known_users"
    __table_args__ = (PrimaryKeyConstraint("project_id", "user_id_hash"),)

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("projects.id"), nullable=False)
    user_id_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

"""Append-only audit records for count-based admission decisions."""

import uuid

from sqlalchemy import JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class WorkAdmissionAudit(Base):
    """One typed admission outcome; it deliberately holds no reservation."""

    __tablename__ = "work_admission_audits"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    subject: Mapped[str] = mapped_column(String(32), index=True)
    outcome: Mapped[str] = mapped_column(String(16), index=True)
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    user_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    reference_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    command_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

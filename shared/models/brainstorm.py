"""Brainstorm model for structured thinking sessions stored in DB."""

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from shared.contracts.dto.brainstorm import BrainstormStatus

from .base import Base


class Brainstorm(Base):
    """Brainstorm — a structured thinking session with status tracking."""

    __tablename__ = "brainstorms"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("projects.id"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(500))
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(50), default=BrainstormStatus.DRAFT.value, index=True
    )
    created_by: Mapped[str] = mapped_column(String(50), default="system")

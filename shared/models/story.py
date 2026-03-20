"""Story model — product-level entity representing what the user wants."""

import uuid

from sqlalchemy import ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from shared.contracts.dto.story import StoryStatus, StoryType

from .base import Base


class Story(Base):
    """Story — a product requirement created by PO, decomposed into Tasks."""

    __tablename__ = "stories"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id"), nullable=False, index=True
    )
    parent_story_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("stories.id"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    acceptance_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[str] = mapped_column(String(50), default=StoryType.PRODUCT.value, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default=StoryStatus.CREATED.value, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0, index=True)
    blocked_by_story_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("stories.id"), nullable=True, index=True
    )
    created_by: Mapped[str] = mapped_column(String(50), default="system")
    user_report: Mapped[str | None] = mapped_column(Text, nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

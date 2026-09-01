"""Immutable user-confirmed product briefs and architect coverage facts."""

import uuid

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ProductBrief(Base):
    __tablename__ = "product_briefs"
    __table_args__ = (UniqueConstraint("project_id", "revision", name="uq_product_brief_revision"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id"), nullable=False, index=True
    )
    story_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("stories.id"), unique=True, nullable=True, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    request_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    confirmed_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmation_request_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True
    )


class RequirementCoverage(Base):
    __tablename__ = "requirement_coverages"
    __table_args__ = (
        UniqueConstraint("brief_id", "requirement_id", name="uq_requirement_coverage"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    brief_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("product_briefs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requirement_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("tasks.id"), nullable=True)
    repository_acceptance_contract: Mapped[str | None] = mapped_column(Text, nullable=True)
    returned_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

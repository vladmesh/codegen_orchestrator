"""Milestone model for grouping work items into phases/epics."""

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from shared.contracts.dto.milestone import MilestoneStatus

from .base import Base


class Milestone(Base):
    """Milestone — a phase or epic that groups work items."""

    __tablename__ = "milestones"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("projects.id"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(50), default=MilestoneStatus.OPEN.value, index=True)
    parent_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("milestones.id"), nullable=True
    )
    created_by: Mapped[str] = mapped_column(String(50), default="system")

"""Task and TaskEvent models for task management (planning layer)."""

from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from shared.contracts.dto.task import (
    TaskEventType,
    TaskStatus,
    TaskType,
)

from .base import Base


class Task(Base):
    """Task — a unit of work with agile statuses (planning layer)."""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("projects.id"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(50), default=TaskType.FEATURE.value)
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default=TaskStatus.BACKLOG.value, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0, index=True)
    acceptance_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_iteration: Mapped[int] = mapped_column(Integer, default=0)
    max_iterations: Mapped[int] = mapped_column(Integer, default=3)
    plan: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(50), default="system")
    source_brainstorm_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("brainstorms.id"), nullable=True
    )
    milestone_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("milestones.id"), nullable=True, index=True
    )


class TaskEvent(Base):
    """TaskEvent — history of status transitions and iteration events."""

    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("tasks.id"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(50), default=TaskEventType.NOTE.value)
    from_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    iteration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    actor: Mapped[str] = mapped_column(String(50), default="system")

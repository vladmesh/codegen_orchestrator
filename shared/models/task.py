"""Task and TaskEvent models for task management (planning layer)."""

import uuid

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, Uuid
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
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id"), nullable=False, index=True
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
    need_e2e: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(50), default="system")
    source_brainstorm_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("brainstorms.id"), nullable=True
    )
    repository_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("repositories.id"), nullable=True
    )
    story_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("stories.id"), nullable=True, index=True
    )
    blocked_by_task_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("tasks.id"), nullable=True
    )
    failure_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    # Whether this task has crossed the coverage-to-dispatch boundary. True for
    # every task that is not planned against an unadmitted Product Brief, which
    # is every task that existed before the boundary did — so a `todo` status
    # keeps meaning "dispatchable" everywhere except under a brief still being
    # planned. Written false only at creation under an active planning attempt,
    # and back to true only by that brief's one admission step.
    dispatch_admitted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # The architect planning attempt this task was planned under, when it was
    # planned against a Product Brief. An admission releases exactly the tasks
    # of its own attempt, so a superseded planner's abandoned tasks are not
    # released by its replacement.
    planning_attempt_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)


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

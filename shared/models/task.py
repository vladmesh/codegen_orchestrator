"""Task model for tracking asynchronous operations."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from shared.contracts.dto.task import TaskStatus  # Single source of truth

from .base import Base


class Task(Base):
    """Task model - tracks asynchronous operations like engineering, deploy, etc."""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    type: Mapped[str] = mapped_column(String(50), index=True)
    status: Mapped[str] = mapped_column(String(50), default=TaskStatus.QUEUED.value, index=True)

    # Associated project
    project_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("projects.id"), nullable=True, index=True
    )

    # Owner (User ID)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True
    )

    # Task metadata (input parameters, configuration)
    # Note: 'metadata' is a reserved name in SQLAlchemy, so we use 'task_metadata'
    task_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)

    # Task result (output data, artifacts, etc.)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Error information if task failed
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_traceback: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timing
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Redis stream for progress events
    callback_stream: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Link to WorkItem (planning layer) — nullable for backward compat
    work_item_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("work_items.id"), nullable=True, index=True
    )
    iteration: Mapped[int | None] = mapped_column(Integer, nullable=True)

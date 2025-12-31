"""Task model for tracking asynchronous operations."""

from datetime import datetime
from enum import Enum

from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class TaskType(str, Enum):
    """Type of task."""

    ENGINEERING = "engineering"
    DEPLOY = "deploy"
    INFRASTRUCTURE = "infrastructure"


class TaskStatus(str, Enum):
    """Task execution status."""

    QUEUED = "queued"  # Task published to queue, waiting to be picked up
    RUNNING = "running"  # Worker is processing the task
    COMPLETED = "completed"  # Task finished successfully
    FAILED = "failed"  # Task failed with error
    CANCELLED = "cancelled"  # Task was cancelled by user


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
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # Redis stream for progress events
    callback_stream: Mapped[str | None] = mapped_column(String(255), nullable=True)

"""Run model for tracking asynchronous operations (execution layer)."""

from datetime import datetime
import uuid

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from shared.contracts.dto.run import RunStatus

from .base import Base


class Run(Base):
    """Run model - tracks asynchronous operations like engineering, deploy, etc."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    type: Mapped[str] = mapped_column(String(50), index=True)
    status: Mapped[str] = mapped_column(String(50), default=RunStatus.QUEUED.value, index=True)

    # Associated project
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id"), nullable=True, index=True
    )

    # Owner (User ID)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True
    )

    # Run metadata (input parameters, configuration)
    # Note: 'metadata' is a reserved name in SQLAlchemy, so we use 'run_metadata'
    run_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)

    # Run result (output data, artifacts, etc.)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Error information if run failed
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_traceback: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timing
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Redis stream for progress events
    callback_stream: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Link to Task (planning layer) — nullable for backward compat
    task_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("tasks.id"), nullable=True, index=True
    )
    iteration: Mapped[int | None] = mapped_column(Integer, nullable=True)

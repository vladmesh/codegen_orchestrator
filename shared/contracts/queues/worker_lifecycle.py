from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class WorkerLifecycleEvent(BaseModel):
    """Worker state change notification from wrapper."""

    worker_id: str
    event: Literal["started", "completed", "failed", "stopped"]
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    result: dict | None = None  # Agent output on success
    error: str | None = None  # Error message on failure
    exit_code: int | None = None

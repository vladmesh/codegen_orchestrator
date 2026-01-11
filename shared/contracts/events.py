from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ProgressEvent(BaseModel):
    """Task progress notification."""

    type: Literal["started", "progress", "completed", "failed"]
    request_id: str
    task_id: str | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    message: str | None = None
    progress_pct: int | None = None
    current_step: str | None = None
    error: str | None = None

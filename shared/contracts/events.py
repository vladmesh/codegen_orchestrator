from datetime import datetime

from pydantic import BaseModel, Field

from shared.contracts.vocab import TaskProgressKind


class ProgressEvent(BaseModel):
    """Task progress notification."""

    type: TaskProgressKind
    request_id: str
    task_id: str | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    message: str | None = None
    progress_pct: int | None = None
    current_step: str | None = None
    error: str | None = None

from datetime import datetime

from pydantic import BaseModel, Field

from shared.contracts.vocab import WorkerLifecycleKind


class WorkerLifecycleEvent(BaseModel):
    """Worker state change notification from wrapper."""

    worker_id: str
    event: WorkerLifecycleKind
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    result: dict | None = None  # Agent output on success
    error: str | None = None  # Error message on failure
    exit_code: int | None = None

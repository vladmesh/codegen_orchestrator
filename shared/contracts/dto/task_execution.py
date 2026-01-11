from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class TaskExecutionDTO(BaseModel):
    """Worker execution record."""

    model_config = ConfigDict(from_attributes=True)

    id: str  # request_id from worker
    task_id: str | None = None  # Optional link to high-level task
    worker_id: str
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    exit_code: int
    status: Literal["success", "failure", "in_progress", "error"]
    result_data: dict[str, Any] | None = None  # AgentVerdict or error details
    created_at: datetime

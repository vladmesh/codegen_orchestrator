from datetime import datetime
import uuid

from pydantic import BaseModel, Field

from shared.contracts.base import BaseResult


class DeveloperWorkerInput(BaseModel):
    """Task for Developer Worker from LangGraph."""

    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str  # Engineering task ID
    project_id: str  # Project UUID
    prompt: str  # Task specification
    timeout: int = 1800  # Max execution time (seconds)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class DeveloperWorkerOutput(BaseResult):
    """Result from Developer Worker to LangGraph."""

    # request_id, status, error, duration_ms inherited from BaseResult
    task_id: str  # Engineering task ID
    commit_sha: str | None = None  # Commit SHA if code was written
    pr_url: str | None = None  # PR URL if created
    timestamp: datetime = Field(default_factory=datetime.utcnow)

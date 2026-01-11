from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from shared.contracts.base import BaseResult


class WorkflowTriggerRequest(BaseModel):
    """Request to trigger GitHub Actions workflow."""

    project_id: str
    repo_full_name: str  # "org/repo"
    workflow_file: str = "main.yml"
    inputs: dict[str, str] = {}  # workflow_dispatch inputs


class WorkflowStatusResult(BaseResult):
    """
    Result of workflow execution.
    Derived from: shared.clients.github.WorkflowRun (GitHub API response).
    """

    run_id: int | None = None
    run_url: str | None = None
    deployed_url: str | None = None
    conclusion: Literal["success", "failure", "cancelled", "skipped"] | None = None


class WorkflowStatusEvent(BaseModel):
    """Progress event for workflow execution."""

    project_id: str
    run_id: int
    status: Literal["queued", "in_progress", "completed"]
    conclusion: Literal["success", "failure", "cancelled", "skipped"] | None = None
    current_step: str | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)

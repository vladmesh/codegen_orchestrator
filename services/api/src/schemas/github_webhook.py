"""The GitHub `workflow_run` delivery members the webhook receiver reads.

GitHub owns the payload; every member not declared here is ignored, so these
projections stay valid as GitHub adds fields.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr


class GitHubWorkflowRunDelivery(BaseModel):
    """Any `workflow_run` delivery: only its action is read."""

    model_config = ConfigDict(extra="ignore")

    action: StrictStr


class GitHubCompletedWorkflowRun(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int = Field(strict=True, gt=0)
    status: Literal["completed"]


class GitHubWorkflowRunCompleted(BaseModel):
    """A `completed` delivery, for any conclusion; its run names no business record."""

    model_config = ConfigDict(extra="ignore")

    action: Literal["completed"]
    workflow_run: GitHubCompletedWorkflowRun

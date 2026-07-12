"""Pydantic models for the worker HTTP result server.

`ResultRequest` is the external contract for the agent's POST /result call.
`to_worker_result` converts a validated request into the typed
:data:`WorkerResult` contract published on the ``worker:{id}:output`` stream.
"""

from pydantic import BaseModel, field_validator, model_validator

from shared.contracts.queues.worker_result import (
    WorkerBlockedResult,
    WorkerCompletedResult,
)


class ResultRequest(BaseModel):
    """POST /result — unified worker result (success or failure).

    success=true requires commit + summary.
    success=false requires reason.
    """

    success: bool
    commit: str | None = None
    summary: str | None = None
    reason: str | None = None

    @field_validator("commit", "summary", "reason", mode="before")
    @classmethod
    def strip_strings(cls, v: str | None) -> str | None:
        if isinstance(v, str) and not v.strip():
            raise ValueError("must not be empty")
        return v

    @model_validator(mode="after")
    def check_required_fields(self) -> "ResultRequest":
        if self.success:
            if not self.commit:
                raise ValueError("commit is required when success=true")
            if not self.summary:
                raise ValueError("summary is required when success=true")
        elif not self.reason:
            raise ValueError("reason is required when success=false")
        return self


def to_worker_result(
    request: ResultRequest,
) -> WorkerCompletedResult | WorkerBlockedResult:
    """Build the typed worker result from a validated HTTP request.

    - success=true  → completed (commit_sha + content)
    - success=false → blocked (block_reason)
    """
    if request.success:
        return WorkerCompletedResult(commit_sha=request.commit, content=request.summary)
    return WorkerBlockedResult(block_reason=request.reason)

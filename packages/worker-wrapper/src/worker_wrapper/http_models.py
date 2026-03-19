"""Pydantic models for the worker HTTP result server.

These models define the request body for POST /result endpoint.
The `to_redis_output` function converts a validated request into
the dict format expected by the worker:{id}:output Redis stream consumer.
"""

from typing import Any

from pydantic import BaseModel, field_validator, model_validator


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


def to_redis_output(request: ResultRequest) -> dict[str, Any]:
    """Convert a validated HTTP request into a Redis output stream dict.

    The output format must match what worker_spawner.py expects:
    - success=true  → {"status": "completed", "commit_sha": ..., "content": ...}
    - success=false → {"status": "blocked", "block_reason": ...}
    """
    if request.success:
        return {
            "status": "completed",
            "commit_sha": request.commit,
            "content": request.summary,
        }
    return {
        "status": "blocked",
        "block_reason": request.reason,
    }

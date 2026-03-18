"""Pydantic models for the worker HTTP result server.

These models define the request bodies for POST /complete, /failed, /blocker
endpoints. The `to_redis_output` function converts a validated request into
the dict format expected by the worker:{id}:output Redis stream consumer.
"""

from typing import Any

from pydantic import BaseModel, field_validator


class CompleteRequest(BaseModel):
    """POST /complete — agent finished the task successfully."""

    commit: str
    summary: str

    @field_validator("commit", "summary")
    @classmethod
    def must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be empty")
        return v


class FailedRequest(BaseModel):
    """POST /failed — agent could not complete the task."""

    reason: str

    @field_validator("reason")
    @classmethod
    def must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be empty")
        return v


class BlockerRequest(BaseModel):
    """POST /blocker — agent is blocked and needs human intervention."""

    reason: str

    @field_validator("reason")
    @classmethod
    def must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be empty")
        return v


def to_redis_output(
    action: str, request: CompleteRequest | FailedRequest | BlockerRequest
) -> dict[str, Any]:
    """Convert a validated HTTP request into a Redis output stream dict.

    The output format must match what worker_spawner.py expects:
    - complete → {"status": "completed", "commit_sha": ..., "content": ...}
    - failed   → {"status": "failed", "error": ...}
    - blocker  → {"status": "blocked", "block_reason": ...}
    """
    if action == "complete" and isinstance(request, CompleteRequest):
        return {
            "status": "completed",
            "commit_sha": request.commit,
            "content": request.summary,
        }
    elif action == "failed" and isinstance(request, FailedRequest):
        return {
            "status": "failed",
            "error": request.reason,
        }
    elif action == "blocker" and isinstance(request, BlockerRequest):
        return {
            "status": "blocked",
            "block_reason": request.reason,
        }
    else:
        raise ValueError(f"Unknown action: {action}")

"""Run schemas (execution layer)."""

from datetime import datetime
from typing import Any
import uuid

from pydantic import AliasChoices, BaseModel, Field

from shared.contracts.dto.base import TimestampedDTO
from shared.contracts.dto.engineering_attempt import EngineeringAttemptLedgerInput

# The create schema is the contract; the API validates against that same object
# rather than a look-alike of its own.
from shared.contracts.dto.run import RunCreate

__all__ = [
    "RunBase",
    "RunCreate",
    "RunRead",
    "RunUpdate",
]


class RunBase(BaseModel):
    """Base run schema."""

    id: str
    type: str
    status: str
    project_id: uuid.UUID | None = None
    user_id: int | None = None
    story_id: str | None = None
    task_id: str | None = None
    run_metadata: dict[str, Any] = {}
    result: dict[str, Any] | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    callback_stream: str | None = None
    iteration: int | None = None
    input_tokens: int | None = Field(
        default=None, validation_alias=AliasChoices("_ledger_input_tokens", "input_tokens")
    )
    output_tokens: int | None = Field(
        default=None, validation_alias=AliasChoices("_ledger_output_tokens", "output_tokens")
    )
    total_tokens: int | None = Field(
        default=None, validation_alias=AliasChoices("_ledger_total_tokens", "total_tokens")
    )
    cost_usd: float | None = Field(
        default=None, validation_alias=AliasChoices("_ledger_cost_usd", "cost_usd")
    )
    agent_profile: dict[str, Any] | None = None
    transcript_path: str | None = None
    transcript_truncated: bool | None = None


class RunRead(RunBase, TimestampedDTO):
    """Schema for reading a run."""


class RunUpdate(BaseModel):
    """Schema for updating a run."""

    status: str | None = None
    # Ownership stamping: a producer that creates work on a user's behalf (a
    # bot-audience rollout, for one) records who it acts for, so the run's own
    # access guard can decide who may read it.
    user_id: int | None = None
    run_metadata: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error_message: str | None = None
    error_traceback: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    iteration: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    agent_profile: dict[str, Any] | None = None
    transcript_path: str | None = None
    transcript_truncated: bool | None = None
    # Only terminal engineering updates may supply this. The API persists it in
    # the same locked transaction as the terminal Run transition.
    engineering_attempt: EngineeringAttemptLedgerInput | None = None

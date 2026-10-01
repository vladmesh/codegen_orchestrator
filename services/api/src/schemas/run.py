"""Run schemas (execution layer)."""

from datetime import datetime
from typing import Any
import uuid

from pydantic import BaseModel, field_validator

from shared.contracts.dto.base import TimestampedDTO
from shared.contracts.dto.engineering_attempt import EngineeringAttemptLedgerInput, QAAccountingFact

# The create schema is the contract; the API validates against that same object
# rather than a look-alike of its own.
from shared.contracts.dto.run import RunCreate, RunStatus, RunType

__all__ = [
    "RunBase",
    "RunCreate",
    "RunRead",
    "RunUpdate",
]


class RunBase(BaseModel):
    """Base run schema."""

    id: str
    type: RunType
    status: RunStatus
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
    agent_profile: dict[str, Any] | None = None
    transcript_path: str | None = None
    transcript_truncated: bool | None = None


class RunRead(RunBase, TimestampedDTO):
    """Schema for reading a run."""

    # Read-only: written only by the story transition that routed this QA run.
    qa_routed_at: datetime | None = None


class RunUpdate(BaseModel):
    """Schema for updating a run."""

    status: RunStatus | None = None
    # Ownership stamping: a producer that creates work on a user's behalf (a
    # durable grant intent, for one) records who it acts for, so the run's own
    # access guard can decide who may read it.
    user_id: int | None = None
    run_metadata: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error_message: str | None = None
    error_traceback: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    iteration: int | None = None
    agent_profile: dict[str, Any] | None = None
    transcript_path: str | None = None
    transcript_truncated: bool | None = None
    # Only terminal engineering updates may supply this. The API persists it in
    # the same locked transaction as the terminal Run transition.
    engineering_attempt: EngineeringAttemptLedgerInput | None = None
    qa_accounting: QAAccountingFact | None = None

    @field_validator("status", mode="before", json_schema_input_type=RunStatus)
    @classmethod
    def _refuse_null_status(cls, value: Any) -> Any:
        # Omitted fields are untouched by PATCH. Explicit null would instead
        # overwrite the non-nullable column, so it is not an accepted input.
        if value is None:
            raise ValueError("status may be omitted but must not be null")
        return value

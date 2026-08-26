"""Typed outcomes of the count-based work admission gate."""

from enum import StrEnum
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from .engineering_budget_policy import EngineeringBudgetAdmissionRead
from .run import RunType


class WorkAdmissionOutcome(StrEnum):
    """A product decision made before new work or infrastructure starts."""

    ADMITTED = "admitted"
    DEFERRED = "deferred"
    DENIED = "denied"


class WorkAdmissionReason(StrEnum):
    """Stable audit reasons for a non-admitted decision."""

    EMERGENCY_STOP = "emergency_stop"
    PROJECT_LIMIT = "project_limit"
    PAID_WORK_LIMIT = "paid_work_limit"
    ENGINEERING_BUDGET_DENIED = "engineering_budget_denied"


class WorkAdmissionRead(BaseModel):
    """Typed result returned to every admission caller."""

    model_config = ConfigDict(from_attributes=True)

    outcome: WorkAdmissionOutcome
    reason: WorkAdmissionReason | None = None
    retryable: bool = False
    message: str | None = None


class EmergencyStopCommand(BaseModel):
    """The operator's exact desired emergency-stop state."""

    enabled: StrictBool


class EmergencyStopRead(BaseModel):
    """Current emergency-stop state."""

    enabled: bool


class WorkAdmissionControlCommand(BaseModel):
    """Typed write for a protected work-admission control."""

    value: StrictBool | StrictInt


class PaidRunStartCommand(BaseModel):
    """The only command that may create a queued paid coding-agent run."""

    id: str
    type: Literal[RunType.ENGINEERING, RunType.QA]
    project_id: uuid.UUID
    story_id: str | None = None
    task_id: str | None = None
    run_metadata: dict[str, Any] = Field(default_factory=dict)
    callback_stream: str | None = None


class PaidRunStartRead(BaseModel):
    """Atomic paid-run start result; an admitted outcome includes its new run."""

    admission: WorkAdmissionRead
    run_id: str | None = None
    engineering_budget: EngineeringBudgetAdmissionRead | None = None

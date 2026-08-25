"""Typed outcomes of the count-based work admission gate."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class WorkAdmissionOutcome(StrEnum):
    """A product decision made before new work or infrastructure starts."""

    ADMITTED = "admitted"
    DEFERRED = "deferred"
    DENIED = "denied"


class WorkAdmissionReason(StrEnum):
    """Stable audit reasons for a non-admitted decision."""

    EMERGENCY_STOP = "emergency_stop"
    PROJECT_LIMIT = "project_limit"
    MANAGED_SERVER_LIMIT = "managed_server_limit"
    PAID_WORK_LIMIT = "paid_work_limit"


class WorkAdmissionRead(BaseModel):
    """Typed result returned to every admission caller."""

    model_config = ConfigDict(from_attributes=True)

    outcome: WorkAdmissionOutcome
    reason: WorkAdmissionReason | None = None
    retryable: bool = False


class EmergencyStopCommand(BaseModel):
    """The operator's exact desired emergency-stop state."""

    enabled: bool


class EmergencyStopRead(BaseModel):
    """Current emergency-stop state."""

    enabled: bool

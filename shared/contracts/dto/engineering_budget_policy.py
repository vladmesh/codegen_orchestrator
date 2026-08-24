"""Wire contract for durable per-user engineering budgets."""

from enum import StrEnum
import uuid

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class EngineeringBudgetPolicyState(StrEnum):
    """Whether a policy's finite limit is enforced."""

    ENABLED = "enabled"
    DISABLED = "disabled"


class EngineeringBudgetReservationState(StrEnum):
    """Lifecycle evidence for an admission reservation."""

    ACTIVE = "active"
    RELEASED = "released"
    UNKNOWN_FINAL = "unknown_final"
    SETTLED = "settled"


class EngineeringBudgetAdmissionOutcome(StrEnum):
    """The durable result of attempting admission for one engineering run."""

    ADMITTED = "admitted"
    DENIED = "denied"
    UNLIMITED = "unlimited"
    NOT_ENFORCED = "not_enforced"


class EngineeringBudgetPolicyCommand(BaseModel):
    """Request the exact resulting state for one user's policy.

    Money is always an integer number of micro-USD.  An existing row only
    changes when ``version`` names its current version; omitting it is valid
    only when creating a row or repeating the state already stored.
    """

    model_config = ConfigDict(extra="forbid")

    limit_microusd: StrictInt = Field(ge=0)
    attempt_reservation_microusd: StrictInt = Field(ge=0)
    state: EngineeringBudgetPolicyState
    version: StrictInt | None = Field(default=None, ge=1)


class EngineeringBudgetAdmissionCommand(BaseModel):
    """Internal dispatch admission command; reserve amount is server policy only."""

    model_config = ConfigDict(extra="forbid")

    attempt_id: str = Field(min_length=1, max_length=255)
    project_id: uuid.UUID
    task_id: str | None = Field(default=None, max_length=255)
    story_id: str | None = Field(default=None, max_length=255)


class EngineeringBudgetAdmissionRead(BaseModel):
    """Idempotent admission result returned before engineering handoff."""

    model_config = ConfigDict(from_attributes=True)

    attempt_id: str
    user_id: int
    outcome: EngineeringBudgetAdmissionOutcome
    reservation_microusd: int
    known_spend_microusd: int
    active_held_microusd: int
    available_microusd: int | None
    reservation_state: EngineeringBudgetReservationState | None

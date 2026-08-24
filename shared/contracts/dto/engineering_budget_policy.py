"""Wire contract for durable per-user engineering budgets."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class EngineeringBudgetPolicyState(StrEnum):
    """Whether a policy's finite limit is enforced."""

    ENABLED = "enabled"
    DISABLED = "disabled"


class EngineeringBudgetPolicyCommand(BaseModel):
    """Request the exact resulting state for one user's policy.

    Money is always an integer number of micro-USD.  An existing row only
    changes when ``version`` names its current version; omitting it is valid
    only when creating a row or repeating the state already stored.
    """

    model_config = ConfigDict(extra="forbid")

    limit_microusd: StrictInt = Field(ge=0)
    state: EngineeringBudgetPolicyState
    version: StrictInt | None = Field(default=None, ge=1)

"""Response schemas for engineering-budget policy and balance endpoints."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from shared.contracts.dto.engineering_budget_policy import EngineeringBudgetPolicyState


class EngineeringBudgetPolicyRead(BaseModel):
    """The durable policy state and its optimistic-lock version."""

    model_config = ConfigDict(from_attributes=True)

    user_id: int
    limit_microusd: int
    state: EngineeringBudgetPolicyState
    version: int


class EngineeringBudgetPolicyLookup(BaseModel):
    """Policy presence and whether a finite limit is currently enforced."""

    user_id: int
    policy: EngineeringBudgetPolicyRead | None
    enforcement: Literal["unlimited", "not_enforced", "enforced"]


class EngineeringBudgetBalanceRead(EngineeringBudgetPolicyLookup):
    """Exact known spend plus explicit unknown-cost coverage."""

    known_spend_microusd: int
    remaining_microusd: int | None
    exhausted: bool
    unknown_cost_attempt_count: int
    incomplete_coverage: bool

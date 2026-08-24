"""Durable per-user engineering-budget policy."""

from sqlalchemy import BigInteger, CheckConstraint, Enum, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from shared.contracts.dto.engineering_budget_policy import EngineeringBudgetPolicyState

from .base import Base


class EngineeringBudgetPolicy(Base):
    """One stable future lock target for a user's engineering budget.

    The append-only engineering-attempt ledger remains the canonical actual
    spend source; this row supplies the per-attempt conservative hold amount.
    """

    __tablename__ = "engineering_budget_policies"
    __table_args__ = (
        CheckConstraint("limit_microusd >= 0", name="ck_engineering_budget_policy_limit"),
        CheckConstraint(
            "attempt_reservation_microusd >= 0",
            name="ck_engineering_budget_policy_attempt_reservation",
        ),
        CheckConstraint("version >= 1", name="ck_engineering_budget_policy_version"),
    )

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), primary_key=True, nullable=False
    )
    limit_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempt_reservation_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    state: Mapped[EngineeringBudgetPolicyState] = mapped_column(
        Enum(
            EngineeringBudgetPolicyState,
            name="engineering_budget_policy_state",
            values_callable=lambda state: [member.value for member in state],
        ),
        nullable=False,
        default=EngineeringBudgetPolicyState.ENABLED,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

"""Durable engineering-budget admission reservations."""

import uuid

from sqlalchemy import BigInteger, CheckConstraint, Enum, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from shared.contracts.dto.engineering_budget_policy import (
    EngineeringBudgetAdmissionOutcome,
    EngineeringBudgetReservationState,
)

from .base import Base


class EngineeringBudgetReservation(Base):
    """One immutable dispatch admission decision and its recoverable hold."""

    __tablename__ = "engineering_budget_reservations"
    __table_args__ = (
        CheckConstraint(
            "reservation_microusd >= 0", name="ck_engineering_budget_reservation_amount"
        ),
        CheckConstraint(
            "known_spend_microusd >= 0", name="ck_engineering_budget_reservation_known_spend"
        ),
        CheckConstraint(
            "active_held_microusd >= 0", name="ck_engineering_budget_reservation_active_held"
        ),
        Index("ix_engineering_budget_reservation_user_state", "user_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    story_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    outcome: Mapped[EngineeringBudgetAdmissionOutcome] = mapped_column(
        Enum(
            EngineeringBudgetAdmissionOutcome,
            name="engineering_budget_admission_outcome",
            values_callable=lambda outcome: [member.value for member in outcome],
        ),
        nullable=False,
    )
    state: Mapped[EngineeringBudgetReservationState | None] = mapped_column(
        Enum(
            EngineeringBudgetReservationState,
            name="engineering_budget_reservation_state",
            values_callable=lambda state: [member.value for member in state],
        ),
        nullable=True,
    )
    reservation_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    known_spend_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    active_held_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

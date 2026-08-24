"""Transactional engineering dispatch budget admission and recovery."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.engineering_budget_policy import (
    EngineeringBudgetAdmissionCommand,
    EngineeringBudgetAdmissionOutcome,
    EngineeringBudgetAdmissionRead,
    EngineeringBudgetPolicyState,
    EngineeringBudgetReservationState,
)
from shared.models import (
    EngineeringAttemptLedger,
    EngineeringBudgetPolicy,
    EngineeringBudgetReservation,
    Project,
)


def _reservation_key(attempt_id: str) -> str:
    return f"engineering-run:{attempt_id}"


def _read(
    reservation: EngineeringBudgetReservation, available: int | None
) -> EngineeringBudgetAdmissionRead:
    return EngineeringBudgetAdmissionRead(
        attempt_id=reservation.attempt_id,
        user_id=reservation.user_id,
        outcome=reservation.outcome,
        reservation_microusd=reservation.reservation_microusd,
        known_spend_microusd=reservation.known_spend_microusd,
        active_held_microusd=reservation.active_held_microusd,
        available_microusd=available,
        reservation_state=reservation.state,
    )


def _same_payload(
    reservation: EngineeringBudgetReservation, command: EngineeringBudgetAdmissionCommand
) -> bool:
    return (
        reservation.project_id == command.project_id
        and reservation.task_id == command.task_id
        and reservation.story_id == command.story_id
    )


async def admit_engineering_attempt(
    command: EngineeringBudgetAdmissionCommand, db: AsyncSession
) -> EngineeringBudgetAdmissionRead:
    """Record exactly one admission decision while holding the policy row lock."""
    existing = await db.scalar(
        select(EngineeringBudgetReservation).where(
            EngineeringBudgetReservation.attempt_id == command.attempt_id
        )
    )
    if existing is not None:
        if not _same_payload(existing, command):
            raise ValueError("Admission attempt identity was reused with a different payload")
        return _read(existing, None)

    project = await db.get(Project, command.project_id)
    if project is None:
        raise LookupError("Project not found")
    policy = await db.scalar(
        select(EngineeringBudgetPolicy)
        .where(EngineeringBudgetPolicy.user_id == project.owner_id)
        .with_for_update()
    )
    # A concurrent caller which waited for this policy lock must see the first
    # result before calculating a second admission.
    existing = await db.scalar(
        select(EngineeringBudgetReservation).where(
            EngineeringBudgetReservation.attempt_id == command.attempt_id
        )
    )
    if existing is not None:
        if not _same_payload(existing, command):
            raise ValueError("Admission attempt identity was reused with a different payload")
        return _read(existing, None)

    known_spend = int(
        await db.scalar(
            select(func.coalesce(func.sum(EngineeringAttemptLedger.cost_microusd), 0)).where(
                EngineeringAttemptLedger.user_id == project.owner_id
            )
        )
        or 0
    )
    active_held = int(
        await db.scalar(
            select(
                func.coalesce(func.sum(EngineeringBudgetReservation.active_held_microusd), 0)
            ).where(
                EngineeringBudgetReservation.user_id == project.owner_id,
                EngineeringBudgetReservation.state.in_(
                    [
                        EngineeringBudgetReservationState.ACTIVE,
                        EngineeringBudgetReservationState.UNKNOWN_FINAL,
                    ]
                ),
            )
        )
        or 0
    )
    if policy is None:
        outcome = EngineeringBudgetAdmissionOutcome.UNLIMITED
        reserve = 0
        state = None
        available = None
    elif policy.state is EngineeringBudgetPolicyState.DISABLED:
        outcome = EngineeringBudgetAdmissionOutcome.NOT_ENFORCED
        reserve = 0
        state = None
        available = None
    else:
        reserve = policy.attempt_reservation_microusd
        available = max(policy.limit_microusd - known_spend - active_held, 0)
        if available <= 0 or reserve > available:
            outcome = EngineeringBudgetAdmissionOutcome.DENIED
            state = None
        else:
            outcome = EngineeringBudgetAdmissionOutcome.ADMITTED
            state = EngineeringBudgetReservationState.ACTIVE
    reservation = EngineeringBudgetReservation(
        idempotency_key=_reservation_key(command.attempt_id),
        attempt_id=command.attempt_id,
        user_id=project.owner_id,
        project_id=command.project_id,
        task_id=command.task_id,
        story_id=command.story_id,
        outcome=outcome,
        state=state,
        reservation_microusd=reserve,
        known_spend_microusd=known_spend,
        active_held_microusd=reserve if state is not None else 0,
    )
    db.add(reservation)
    return _read(reservation, available)


async def release_pre_handoff_reservation(attempt_id: str, db: AsyncSession) -> None:
    """Release only a hold whose queue handoff definitely did not happen."""
    reservation = await db.scalar(
        select(EngineeringBudgetReservation)
        .where(EngineeringBudgetReservation.attempt_id == attempt_id)
        .with_for_update()
    )
    if reservation is not None and reservation.state is EngineeringBudgetReservationState.ACTIVE:
        reservation.state = EngineeringBudgetReservationState.RELEASED
        reservation.active_held_microusd = 0


async def finalize_engineering_reservation(
    attempt_id: str, cost_microusd: int | None, db: AsyncSession
) -> None:
    """Settle known terminal cost or retain a conservative unknown-final hold."""
    reservation = await db.scalar(
        select(EngineeringBudgetReservation)
        .where(EngineeringBudgetReservation.attempt_id == attempt_id)
        .with_for_update()
    )
    if reservation is None or reservation.state in {
        EngineeringBudgetReservationState.RELEASED,
        EngineeringBudgetReservationState.UNKNOWN_FINAL,
        EngineeringBudgetReservationState.SETTLED,
    }:
        return
    if cost_microusd is None:
        reservation.state = EngineeringBudgetReservationState.UNKNOWN_FINAL
        reservation.active_held_microusd = reservation.reservation_microusd
    else:
        reservation.state = EngineeringBudgetReservationState.SETTLED
        reservation.active_held_microusd = 0

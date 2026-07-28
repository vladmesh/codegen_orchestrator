"""Temporary access grants router: the durable record revocation is driven from."""

from datetime import UTC, datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.temporary_access import (
    LIVE_TEMPORARY_ACCESS_STATUSES,
    REVOKE_CONFIRMATION_READINGS,
    REVOKE_CONFIRMATION_WINDOW,
    TemporaryAccessObservation,
    TemporaryAccessRevokeReason,
    TemporaryAccessStatus,
)
from shared.models import Run, TemporaryAccessGrant

from ..database import get_async_session
from ..dependencies import require_internal_or_admin
from ..schemas import (
    TemporaryAccessEscalation,
    TemporaryAccessGrantCreate,
    TemporaryAccessGrantRead,
    TemporaryAccessGrantUpdate,
)

router = APIRouter(prefix="/temporary-access-grants", tags=["temporary-access"])


async def _load(grant_id: str, db: AsyncSession, *, lock: bool = False) -> TemporaryAccessGrant:
    """Read a grant, optionally with its row locked for the rest of the transaction.

    Every caller that decides something from the grant's current status locks it.
    Two sweep ticks, or a sweep and a retry, otherwise read the same live grant
    and both act on a status the other is already replacing.
    """
    grant = await db.get(TemporaryAccessGrant, grant_id, with_for_update=lock)
    if grant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Temporary access grant {grant_id} not found",
        )
    return grant


@router.post("/", response_model=TemporaryAccessGrantRead, status_code=status.HTTP_201_CREATED)
async def create_grant(
    grant_in: TemporaryAccessGrantCreate,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> TemporaryAccessGrant:
    """Record a grant before the access is handed out.

    Registering the same grant twice returns the stored one instead of failing:
    the caller writes this record before its external effect, so a retry after a
    crash must be able to reach the same state. A different grant competing for
    the same live contract slot is refused, because the slot holds one value and
    the loser could never be revoked.
    """
    existing = await db.get(TemporaryAccessGrant, grant_in.id)
    if existing is not None:
        return existing

    live = await db.execute(
        select(TemporaryAccessGrant).where(
            TemporaryAccessGrant.project_id == grant_in.project_id,
            TemporaryAccessGrant.env_key == grant_in.env_key,
            TemporaryAccessGrant.status != TemporaryAccessStatus.REVOKED.value,
        )
    )
    held = live.scalar_one_or_none()
    if held is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Temporary access for {grant_in.env_key} is already granted by "
                f"{held.id} (run {held.qa_run_id}, status {held.status})"
            ),
        )

    grant = TemporaryAccessGrant(
        id=grant_in.id,
        project_id=grant_in.project_id,
        env_key=grant_in.env_key,
        subject=grant_in.subject,
        head_sha=grant_in.head_sha,
        qa_run_id=grant_in.qa_run_id,
        grant_run_id=grant_in.grant_run_id,
        qa_message=grant_in.qa_message.model_dump(mode="json"),
        status=TemporaryAccessStatus.GRANTING.value,
        granted_at=datetime.now(UTC),
    )
    db.add(grant)
    await db.commit()
    await db.refresh(grant)
    return grant


@router.get("/", response_model=list[TemporaryAccessGrantRead])
async def list_grants(
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
    project_id: uuid.UUID | None = Query(None),
    qa_run_id: str | None = Query(None, description="Grants made for one QA run"),
    grant_status: list[TemporaryAccessStatus] | None = Query(None, alias="status"),
    live: bool = Query(False, description="Only grants that still hold access"),
    revoked_after: datetime | None = Query(
        None,
        description=(
            "With live=true, also return grants closed no earlier than this moment, "
            "so the sweep keeps watching a slot for a while after it was confirmed empty"
        ),
    ),
) -> list[TemporaryAccessGrant]:
    """List grants, newest first.

    ``live`` and ``revoked_after`` widen one set rather than narrowing it. A
    closed grant is not holding access, but the value can still come back onto
    the running service after the readings agreed it was gone — a dispatch that
    was already on its way, or a write from outside — and nothing would notice if
    the sweep stopped looking the moment the record closed. So the sweep asks for
    the live grants plus the recently closed ones, and reads the slot of both.
    """
    query = select(TemporaryAccessGrant).order_by(TemporaryAccessGrant.granted_at.desc())
    if project_id is not None:
        query = query.where(TemporaryAccessGrant.project_id == project_id)
    if qa_run_id is not None:
        query = query.where(TemporaryAccessGrant.qa_run_id == qa_run_id)
    if grant_status:
        query = query.where(TemporaryAccessGrant.status.in_([item.value for item in grant_status]))
    if live:
        held = TemporaryAccessGrant.status.in_(
            [item.value for item in LIVE_TEMPORARY_ACCESS_STATUSES]
        )
        if revoked_after is not None:
            held = or_(
                held,
                and_(
                    TemporaryAccessGrant.status == TemporaryAccessStatus.REVOKED.value,
                    TemporaryAccessGrant.revoked_at >= revoked_after,
                ),
            )
        query = query.where(held)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.get("/{grant_id}", response_model=TemporaryAccessGrantRead)
async def get_grant(
    grant_id: str,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> TemporaryAccessGrant:
    """Read one grant."""
    return await _load(grant_id, db)


@router.post("/{grant_id}/escalate", response_model=TemporaryAccessGrantRead)
async def escalate_grant(
    grant_id: str,
    escalation: TemporaryAccessEscalation,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> TemporaryAccessGrant:
    """Record that the access could not be taken back, on the grant and on its run.

    A QA run that borrowed a test identity is not over when the worker inside it
    has an opinion: the identity it was lent still has to be handed back, and
    until that is settled the verdict is provisional. So when the sweep runs out
    of revoke attempts, this writes the named cleanup failure onto the run even
    if the worker already recorded a pass — the story would otherwise publish a
    success while the identity is still admitted by the deployed bot, or, if
    nothing dared write, wait in TESTING for a revoke that keeps failing.

    This is the one writer allowed the last word on a QA run's outcome, and only
    this one thing. Everything else that reaches ``PATCH /runs/{id}`` is still
    refused once a terminal run carries a result — that guard exists to stop a
    worker's late verdict from erasing a supervisor's, and it holds in that
    direction unchanged. A worker reporting after this escalation is refused, so
    the two orders end in the same place.

    Both writes commit together, and both rows are locked before either is read.
    Being allowed the last word is not the same as getting it: a QA worker's
    ``PATCH`` that read the run as still running would otherwise commit after
    this one and put its pass back over the named cleanup failure. Under the lock
    that ``PATCH`` reads the failure this wrote and is refused by the ordinary
    outcome rule. Repeating this call is the same state again: the escalation
    moment is stamped once and the run's outcome is rewritten to the same values.
    """
    grant = await _load(grant_id, db, lock=True)
    if grant.status == TemporaryAccessStatus.REVOKED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Temporary access grant {grant_id} is revoked; there is nothing to escalate",
        )

    run = await db.get(Run, grant.qa_run_id, with_for_update=True)
    if run is not None:
        run.status = RunStatus.FAILED.value
        run.error_message = escalation.run_error_message
        run.result = escalation.run_result.model_dump(mode="json")
        if run.completed_at is None:
            run.completed_at = datetime.now(UTC)

    grant.status = TemporaryAccessStatus.REVOKE_FAILED.value
    grant.last_error = escalation.error
    if grant.escalated_at is None:
        grant.escalated_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(grant)
    return grant


@router.post("/{grant_id}/observation", response_model=TemporaryAccessGrantRead)
async def record_observation(
    grant_id: str,
    observation: TemporaryAccessObservation,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> TemporaryAccessGrant:
    """Record what the running service was seen holding, and close the grant on it.

    This is the only way to REVOKED. A deploy that cleared the value is a request
    accepted by GitHub Actions, not an effect: the workflow runs when it runs, and
    a dispatch already on its way can write the identity back afterwards. So the
    record follows the server rather than the request, and this is where the
    server gets a say.

    The reading has to be of the right machine. A project may run on several
    servers, and the QA run borrowed the identity on exactly one application —
    the one its handoff names. A clear slot somewhere else says nothing about the
    bot that was tested, so a reading of another application is refused.

    One clear reading still closes nothing. It is a moment, and a late writer
    lands after moments; so the grant stays under reconciliation, being read
    again, until enough readings taken over ``REVOKE_CONFIRMATION_WINDOW`` have
    agreed. A reading that finds the value again puts the streak back to the
    start, whether the value was never removed or was written back by something
    outside — from here the two are the same disagreement and get the same
    answer, which is that the access is still out.

    Closing the grant does not end the readings. A grant that has been closed is
    still read for as long as the sweep keeps it under watch, and a reading that
    finds the value on it puts it back to REVOKING under
    ``OBSERVED_AFTER_REVOKE`` — a dispatch that landed after the confirmation and
    a write from outside are the same thing seen from here, a value that should
    not be there, and the answer to both is to take it off again.

    Repeating one reading is not two: the id it carries is stored, and an
    observation the caller already delivered is returned as the no-op it is.
    """
    grant = await _load(grant_id, db, lock=True)

    if observation.env_key != grant.env_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Grant {grant_id} holds {grant.env_key}; the reading is of {observation.env_key}"
            ),
        )
    expected_application = grant.qa_message["application_id"]
    if observation.application_id != expected_application:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Grant {grant_id} was tested on application {expected_application}; "
                f"the reading is of application {observation.application_id}"
            ),
        )

    if grant.status not in (
        TemporaryAccessStatus.REVOKING.value,
        TemporaryAccessStatus.REVOKED.value,
    ):
        # Nothing to settle. A grant that is still meant to hold the access is
        # not waiting for a reading, and an empty slot under it is a broken
        # grant deploy for the sweep to decide, not something to fold into this
        # record silently.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Temporary access grant {grant_id} is {grant.status}; "
                "only a grant being revoked or already closed is read"
            ),
        )

    if grant.observation_id == observation.observation_id:
        return grant

    now = datetime.now(UTC)
    grant.observation_id = observation.observation_id
    grant.observed_at = now

    if grant.status == TemporaryAccessStatus.REVOKED.value:
        return await _read_a_closed_grant(grant, observation, now, db)

    if observation.present:
        grant.slot_clear_since = None
        grant.slot_clear_readings = 0
        await db.commit()
        await db.refresh(grant)
        return grant

    if grant.slot_clear_since is None:
        grant.slot_clear_since = now
        grant.slot_clear_readings = 1
    else:
        grant.slot_clear_readings += 1

    confirmed = (
        grant.slot_clear_readings >= REVOKE_CONFIRMATION_READINGS
        and now - grant.slot_clear_since >= REVOKE_CONFIRMATION_WINDOW
    )
    if confirmed:
        if grant.revoke_reason is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Temporary access grant {grant_id} is revoking without a reason",
            )
        grant.status = TemporaryAccessStatus.REVOKED.value
        grant.revoked_at = now

    await db.commit()
    await db.refresh(grant)
    return grant


async def _read_a_closed_grant(
    grant: TemporaryAccessGrant,
    observation: TemporaryAccessObservation,
    now: datetime,
    db: AsyncSession,
) -> TemporaryAccessGrant:
    """Fold a reading taken after the grant was closed into the record.

    An empty slot is the confirmation holding, and only the moment of the reading
    is worth keeping — it is what paces the next question and what makes each
    reading a separate one.

    A slot that is filled again is a value that should not be there. The grant
    goes back to REVOKING and the sweep takes it off, with the retry budget
    counted from the reopening: this is a new disagreement, not the continuation
    of one that was already settled.

    Unless the slot has an owner. The contract has one slot per (project, key),
    so a later grant may already be holding it on purpose, and what is being read
    is that grant's value rather than this one's leftover. Reopening then would
    make two grants revoke each other. The reading is kept and the slot is left
    to the grant that owns it, which is under reconciliation itself.
    """
    if not observation.present:
        grant.slot_clear_readings += 1
        await db.commit()
        await db.refresh(grant)
        return grant

    owner = await db.execute(
        select(TemporaryAccessGrant).where(
            TemporaryAccessGrant.project_id == grant.project_id,
            TemporaryAccessGrant.env_key == grant.env_key,
            TemporaryAccessGrant.status != TemporaryAccessStatus.REVOKED.value,
        )
    )
    if owner.scalar_one_or_none() is None:
        grant.status = TemporaryAccessStatus.REVOKING.value
        grant.revoke_reason = TemporaryAccessRevokeReason.OBSERVED_AFTER_REVOKE.value
        grant.revoked_at = None
        grant.reopened_at = now
        grant.revoke_attempts = 0
        grant.slot_clear_since = None
        grant.slot_clear_readings = 0
        grant.last_error = (
            f"{observation.env_key} is set on application {observation.application_id} "
            f"after the grant was confirmed revoked"
        )

    await db.commit()
    await db.refresh(grant)
    return grant


@router.patch("/{grant_id}", response_model=TemporaryAccessGrantRead)
async def update_grant(
    grant_id: str,
    update: TemporaryAccessGrantUpdate,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> TemporaryAccessGrant:
    """Move a grant along its lifecycle, short of closing it.

    A revoked grant is terminal evidence and nothing here reopens it. Neither can
    anything here produce it: the schema refuses REVOKED, because that status is
    a statement about the deployed service that only a reading of the server can
    make. See ``record_observation``.
    """
    grant = await _load(grant_id, db, lock=True)
    fields = update.model_dump(exclude_unset=True)

    if grant.status == TemporaryAccessStatus.REVOKED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Temporary access grant {grant_id} is already revoked",
        )

    new_status = fields.pop("status", None)

    # Both moments are stamped once. The sweep re-asks after a crash it cannot
    # remember, and the first answer is the one that happened.
    if fields.pop("qa_dispatched", None) and grant.qa_dispatched_at is None:
        grant.qa_dispatched_at = datetime.now(UTC)
    if fields.pop("escalated", None) and grant.escalated_at is None:
        grant.escalated_at = datetime.now(UTC)

    for key, value in fields.items():
        setattr(grant, key, value.value if hasattr(value, "value") else value)
    if new_status is not None:
        grant.status = new_status.value

    await db.commit()
    await db.refresh(grant)
    return grant

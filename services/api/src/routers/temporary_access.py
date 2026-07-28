"""Temporary access grants router: the durable record revocation is driven from."""

from datetime import UTC, datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.temporary_access import (
    LIVE_TEMPORARY_ACCESS_STATUSES,
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
) -> list[TemporaryAccessGrant]:
    """List grants, newest first."""
    query = select(TemporaryAccessGrant).order_by(TemporaryAccessGrant.granted_at.desc())
    if project_id is not None:
        query = query.where(TemporaryAccessGrant.project_id == project_id)
    if qa_run_id is not None:
        query = query.where(TemporaryAccessGrant.qa_run_id == qa_run_id)
    if grant_status:
        query = query.where(TemporaryAccessGrant.status.in_([item.value for item in grant_status]))
    if live:
        query = query.where(
            TemporaryAccessGrant.status.in_([item.value for item in LIVE_TEMPORARY_ACCESS_STATUSES])
        )
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


@router.patch("/{grant_id}", response_model=TemporaryAccessGrantRead)
async def update_grant(
    grant_id: str,
    update: TemporaryAccessGrantUpdate,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> TemporaryAccessGrant:
    """Move a grant along, or confirm it is already where the caller wants it.

    Revoking an already revoked grant is the expected outcome of a retry, not an
    error: the caller asked for access to be gone and it is gone. Any other write
    to a revoked grant is refused, because the record is terminal evidence.
    """
    grant = await _load(grant_id, db, lock=True)
    fields = update.model_dump(exclude_unset=True)

    if grant.status == TemporaryAccessStatus.REVOKED.value:
        if fields.get("status") is TemporaryAccessStatus.REVOKED:
            return grant
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Temporary access grant {grant_id} is already revoked",
        )

    new_status = fields.pop("status", None)
    if new_status is TemporaryAccessStatus.REVOKED:
        reason = fields.get("revoke_reason") or grant.revoke_reason
        if reason is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="revoke_reason is required to revoke a grant",
            )
        grant.revoked_at = datetime.now(UTC)

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

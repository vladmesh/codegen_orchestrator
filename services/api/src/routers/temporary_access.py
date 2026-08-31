"""Durable temporary QA admission records."""

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
from shared.contracts.queues.deploy import DeployOutcome
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
_TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}
)
_LEGACY_REMEDIATION = (
    "A legacy temporary QA access grant is still live. Let the prior release drain it before "
    "enabling capability-backed QA access."
)


def _is_legacy(grant: TemporaryAccessGrant) -> bool:
    """A target-less row belongs to the retired environment-slot lifecycle."""
    return grant.target_base_url is None


def _reject_legacy_record(
    grant: TemporaryAccessGrant, *, allow_revoked_history: bool = False
) -> None:
    if _is_legacy(grant) and not (
        allow_revoked_history and grant.status == TemporaryAccessStatus.REVOKED.value
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_LEGACY_REMEDIATION)


async def _load(grant_id: str, db: AsyncSession, *, lock: bool = False) -> TemporaryAccessGrant:
    grant = await db.get(TemporaryAccessGrant, grant_id, with_for_update=lock)
    if grant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Temporary access grant not found"
        )
    return grant


async def _reject_live_legacy(db: AsyncSession) -> None:
    legacy = await db.scalar(
        select(TemporaryAccessGrant.id)
        .where(
            TemporaryAccessGrant.target_base_url.is_(None),
            TemporaryAccessGrant.status != TemporaryAccessStatus.REVOKED.value,
        )
        .limit(1)
    )
    if legacy is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_LEGACY_REMEDIATION)


async def _require_proved_operation(db: AsyncSession, run_id: str | None, *, expected: str) -> None:
    if run_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Temporary access {expected} has no recorded capability operation",
        )
    run = await db.get(Run, run_id)
    outcome = (run.result or {}).get("deploy_outcome") if run is not None else None
    if (
        run is None
        or run.status != RunStatus.COMPLETED.value
        or outcome != DeployOutcome.SUCCESS.value
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Temporary access {expected} has not proved its required access readback",
        )


@router.post("/", response_model=TemporaryAccessGrantRead, status_code=status.HTTP_201_CREATED)
async def create_grant(
    grant_in: TemporaryAccessGrantCreate,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> TemporaryAccessGrant:
    """Persist the exact identity and target before a capability call is queued."""
    existing = await db.get(TemporaryAccessGrant, grant_in.id)
    if existing is not None:
        # An id from the retired slot lifecycle can collide with the durable
        # capability id for the same QA run. It is history, not a record a
        # capability caller may hydrate or continue.
        _reject_legacy_record(existing)
        return existing
    await _reject_live_legacy(db)
    held = await db.scalar(
        select(TemporaryAccessGrant).where(
            TemporaryAccessGrant.project_id == grant_in.project_id,
            TemporaryAccessGrant.target_application_id == grant_in.target_application_id,
            TemporaryAccessGrant.status != TemporaryAccessStatus.REVOKED.value,
        )
    )
    if held is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Temporary QA access for target {grant_in.target_application_id} "
                f"is held by {held.id}"
            ),
        )
    grant = TemporaryAccessGrant(
        id=grant_in.id,
        project_id=grant_in.project_id,
        channel=grant_in.channel,
        external_id=grant_in.external_id,
        target_application_id=grant_in.target_application_id,
        target_base_url=grant_in.target_base_url,
        head_sha=grant_in.head_sha,
        qa_run_id=grant_in.qa_run_id,
        grant_run_id=grant_in.grant_run_id,
        grant_attempts=1,
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
    qa_run_id: str | None = Query(None),
    grant_status: list[TemporaryAccessStatus] | None = Query(None, alias="status"),
    live: bool = Query(False),
) -> list[TemporaryAccessGrant]:
    # A live legacy row blocks only capability-backed creation. It must not
    # prevent this sweep from reconciling unrelated, target-bound records.
    query = select(TemporaryAccessGrant)
    # Recovery asks whether this QA run ever had a lifecycle. Include legacy
    # history for that narrow lookup so it cannot re-publish a handoff after a
    # recorded slot lifecycle. General capability reconciliation never obtains
    # target-less rows.
    if qa_run_id is None:
        query = query.where(TemporaryAccessGrant.target_base_url.is_not(None))
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
    query = query.order_by(TemporaryAccessGrant.granted_at.desc())
    return list((await db.execute(query)).scalars().all())


@router.get("/{grant_id}", response_model=TemporaryAccessGrantRead)
async def get_grant(
    grant_id: str,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> TemporaryAccessGrant:
    grant = await _load(grant_id, db)
    _reject_legacy_record(grant, allow_revoked_history=True)
    return grant


@router.patch("/{grant_id}", response_model=TemporaryAccessGrantRead)
async def update_grant(
    grant_id: str,
    update: TemporaryAccessGrantUpdate,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> TemporaryAccessGrant:
    grant = await _load(grant_id, db, lock=True)
    _reject_legacy_record(grant)
    for field, value in update.model_dump(exclude_unset=True).items():
        if field == "qa_dispatched":
            if value and grant.qa_dispatched_at is None:
                grant.qa_dispatched_at = datetime.now(UTC)
        elif field == "escalated":
            if value and grant.escalated_at is None:
                grant.escalated_at = datetime.now(UTC)
        elif field == "status" and value is not None:
            if value is TemporaryAccessStatus.GRANTED:
                await _require_proved_operation(db, grant.grant_run_id, expected="grant")
            if value is TemporaryAccessStatus.REVOKED:
                await _require_proved_operation(db, grant.revoke_run_id, expected="revoke")
            grant.status = value.value
            if value is TemporaryAccessStatus.REVOKED and grant.revoked_at is None:
                grant.revoked_at = datetime.now(UTC)
        elif value is not None:
            setattr(grant, field, value.value if hasattr(value, "value") else value)
    await db.commit()
    await db.refresh(grant)
    return grant


@router.post("/{grant_id}/escalate", response_model=TemporaryAccessGrantRead)
async def escalate_grant(
    grant_id: str,
    escalation: TemporaryAccessEscalation,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> TemporaryAccessGrant:
    grant = await _load(grant_id, db, lock=True)
    _reject_legacy_record(grant)
    run = await db.get(Run, grant.qa_run_id, with_for_update=True)
    if run is not None and run.status not in _TERMINAL_RUN_STATUSES:
        run.status = RunStatus.FAILED.value
        run.error_message = escalation.run_error_message
        run.result = escalation.run_result.model_dump(mode="json")
        run.completed_at = run.completed_at or datetime.now(UTC)
    grant.status = TemporaryAccessStatus.REVOKE_FAILED.value
    grant.last_error = escalation.error
    grant.escalated_at = grant.escalated_at or datetime.now(UTC)
    await db.commit()
    await db.refresh(grant)
    return grant

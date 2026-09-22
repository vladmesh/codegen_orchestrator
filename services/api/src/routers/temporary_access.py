"""Durable temporary QA admission records."""

from datetime import UTC, datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.qa_handoff import QA_ROUTED_KEY
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.temporary_access import (
    LIVE_TEMPORARY_ACCESS_STATUSES,
    QA_ROUTING_PENDING,
    TemporaryAccessDrainReason,
    TemporaryAccessRevokeReason,
    TemporaryAccessStatus,
)
from shared.contracts.queues.deploy import DeployOutcome
from shared.models import Run, TemporaryAccessGrant, WorkAdmissionAudit

from ..database import get_async_session
from ..dependencies import get_internal_or_admin_actor, require_internal_or_admin
from ..schemas import (
    TemporaryAccessDrainCommand,
    TemporaryAccessEscalation,
    TemporaryAccessGrantCreate,
    TemporaryAccessGrantRead,
    TemporaryAccessGrantUpdate,
)

router = APIRouter(prefix="/temporary-access-grants", tags=["temporary-access"])
_TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}
)
_DRAIN_AUDIT_SUBJECT = "temporary_access_drain"


async def _load(grant_id: str, db: AsyncSession, *, lock: bool = False) -> TemporaryAccessGrant:
    grant = await db.get(TemporaryAccessGrant, grant_id, with_for_update=lock)
    if grant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Temporary access grant not found"
        )
    return grant


def _is_complete_current_target(grant: TemporaryAccessGrant) -> bool:
    return (
        grant.channel is not None
        and bool(grant.channel.strip())
        and grant.external_id is not None
        and bool(grant.external_id.strip())
        and grant.target_application_id > 0
        and bool(grant.target_base_url.strip())
    )


async def _existing_drain_audit(db: AsyncSession, grant_id: str) -> WorkAdmissionAudit | None:
    return await db.scalar(
        select(WorkAdmissionAudit)
        .where(
            WorkAdmissionAudit.subject == _DRAIN_AUDIT_SUBJECT,
            WorkAdmissionAudit.reference_id == grant_id,
        )
        .limit(1)
    )


async def _awaits_story_routing(db: AsyncSession, run: Run) -> bool:
    """Whether a QA verdict is still owed to its story's routing.

    Called under the QA run's row lock. The story transition that consumes the
    verdict stamps the run under the same lock, so this cannot read "unrouted"
    and then have the escalation commit after a routing that already happened.
    A run a newer QA run of the story has superseded is never routed: routing
    reads the newest run only.
    """
    if run.story_id is None or run.status not in _TERMINAL_RUN_STATUSES:
        return False
    if not isinstance(run.result, dict) or run.result.get("qa_outcome") is None:
        return False
    routed = (run.run_metadata or {}).get(QA_ROUTED_KEY)
    if isinstance(routed, dict) and routed.get("story_id") == run.story_id:
        return False
    newer = await db.scalar(
        select(Run.id)
        .where(
            Run.story_id == run.story_id,
            Run.type == RunType.QA.value,
            Run.created_at > run.created_at,
        )
        .limit(1)
    )
    return newer is None


def _admin_user_id(actor: str) -> int | None:
    if not actor.startswith("admin:"):
        return None
    return int(actor.removeprefix("admin:"))


async def _require_proved_operation(db: AsyncSession, run_id: str | None, *, expected: str) -> None:
    if run_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Temporary access {expected} has no recorded capability operation",
        )
    run = await db.get(Run, run_id)
    result = (run.result or {}) if run is not None else {}
    # A skipped deploy never reached the product, so its SUCCESS proves no readback.
    if (
        run is None
        or run.status != RunStatus.COMPLETED.value
        or result.get("deploy_outcome") != DeployOutcome.SUCCESS.value
        or result.get("skipped_reason") is not None
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
        return existing
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
    query = select(TemporaryAccessGrant)
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
    return await _load(grant_id, db)


@router.post("/{grant_id}/drain", response_model=TemporaryAccessGrantRead)
async def drain_grant(
    grant_id: str,
    command: TemporaryAccessDrainCommand,
    db: AsyncSession = Depends(get_async_session),
    actor: str = Depends(get_internal_or_admin_actor),
) -> TemporaryAccessGrant:
    """Close only cleanup the reconciler already escalated as unprovable."""
    grant = await _load(grant_id, db, lock=True)
    if grant.status == TemporaryAccessStatus.REVOKED.value:
        audit = await _existing_drain_audit(db, grant.id)
        if (
            audit is not None
            and audit.command_payload == command.model_dump(mode="json")
            and grant.revoke_reason == TemporaryAccessDrainReason.OPERATOR_DRAIN.value
            and grant.revoked_at is not None
        ):
            return grant
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Temporary access grant was not closed by this operator drain",
        )

    try:
        stored_status = TemporaryAccessStatus(grant.status)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Temporary access grant has malformed lifecycle state",
        ) from exc

    try:
        stored_revoke_reason = (
            TemporaryAccessRevokeReason(grant.revoke_reason)
            if grant.revoke_reason is not None
            else None
        )
    except ValueError:
        stored_revoke_reason = None

    escalated_eligible = (
        _is_complete_current_target(grant)
        and stored_status is TemporaryAccessStatus.REVOKE_FAILED
        and grant.escalated_at is not None
        and bool(grant.revoke_run_id)
        and grant.revoke_attempts >= 1
        and stored_revoke_reason is not None
        and bool(grant.last_error)
    )
    if not escalated_eligible:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Operator drain requires an escalated target-backed revoke_failed grant",
        )

    before_status = grant.status
    grant.status = TemporaryAccessStatus.REVOKED.value
    grant.revoke_reason = command.reason.value
    grant.revoked_at = datetime.now(UTC)
    db.add(
        WorkAdmissionAudit(
            subject=_DRAIN_AUDIT_SUBJECT,
            outcome="drained",
            reason=command.reason.value,
            user_id=_admin_user_id(actor),
            reference_id=grant.id,
            command_payload=command.model_dump(mode="json"),
            message="Operator accepted unproved remote temporary-access cleanup",
            before_value={"status": before_status},
            after_value={"status": TemporaryAccessStatus.REVOKED.value},
            actor=actor,
        )
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
    grant = await _load(grant_id, db, lock=True)
    for field, value in update.model_dump(exclude_unset=True).items():
        if field == "qa_dispatched":
            if value and grant.qa_dispatched_at is None:
                grant.qa_dispatched_at = datetime.now(UTC)
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
    run = await db.get(Run, grant.qa_run_id, with_for_update=True)
    if grant.escalated_at is None and run is not None and await _awaits_story_routing(db, run):
        # The cleanup incident waits until the story has consumed this verdict;
        # the scheduler keeps cleaning up and asks again on a later cycle.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=QA_ROUTING_PENDING,
        )
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

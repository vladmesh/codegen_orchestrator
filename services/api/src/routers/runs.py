"""Runs router (execution layer)."""

from datetime import UTC, datetime
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.deploy_dispatch import (
    DISPATCH_CLAIMED_AT_KEY,
    DISPATCH_LEASE,
    DISPATCH_LEASE_EXPIRES_AT_KEY,
    DISPATCH_SUPERSEDED_AT_KEY,
    DeployDispatchClaim,
    DeployDispatchSupersede,
    DeployDispatchWithdrawal,
    DeployRunStart,
    DispatchSupersede,
    DispatchWithdrawal,
)
from shared.contracts.dto.run import RunStatus
from shared.models import Run, User

from ..database import get_async_session
from ..dependencies import is_internal_service, require_internal_or_admin
from ..schemas import RunCreate, RunRead, RunUpdate

logger = structlog.get_logger()

router = APIRouter(prefix="/runs", tags=["runs"])

# A run in one of these has produced its outcome; nothing may start work for it.
_TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}
)

# What a run says happened. Once a terminal run carries a result, these are the
# fields nothing may rewrite.
_OUTCOME_FIELDS = ("status", "result", "error_message")


async def _resolve_user(
    telegram_id: int | None,
    db: AsyncSession,
) -> User | None:
    """Resolve User from telegram_id."""
    if not telegram_id:
        return None
    query = select(User).where(User.telegram_id == telegram_id)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def _check_run_access(
    run: Run,
    telegram_id: int | None,
    db: AsyncSession,
    *,
    is_internal: bool = False,
) -> None:
    """Check if user has access to run. Raises 401/403 if denied."""
    if is_internal:
        return
    if telegram_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    user = await _resolve_user(telegram_id, db)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with telegram_id {telegram_id} not found",
        )

    if user.is_admin:
        return

    # Regular user: must be owner
    if run.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: not run owner",
        )


@router.post("/", response_model=RunRead, status_code=status.HTTP_201_CREATED)
async def create_run(
    run: RunCreate,
    db: AsyncSession = Depends(get_async_session),
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
) -> Run:
    """Create a new run."""
    # Verify user exists if user_id provided
    if run.user_id:
        query = select(User).where(User.id == run.user_id)
        result = await db.execute(query)
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with id {run.user_id} not found",
            )

    db_run = Run(**run.model_dump())
    db.add(db_run)
    await db.commit()
    await db.refresh(db_run)

    logger.info(
        "run_created",
        run_id=db_run.id,
        run_type=db_run.type,
        user_id=db_run.user_id,
    )

    return db_run


@router.get("/{run_id}", response_model=RunRead)
async def get_run(
    run_id: str,
    db: AsyncSession = Depends(get_async_session),
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    _is_internal: bool = Depends(is_internal_service),
) -> Run:
    """Get run by ID."""
    query = select(Run).where(Run.id == run_id)
    result = await db.execute(query)
    run = result.scalar_one_or_none()

    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )

    await _check_run_access(run, x_telegram_id, db, is_internal=_is_internal)

    return run


@router.get("/", response_model=list[RunRead])
async def list_runs(
    project_id: uuid.UUID | None = None,
    task_id: str | None = None,
    story_id: str | None = None,
    run_type: str | None = None,
    # alias keeps the public query param name; a parameter literally named
    # `status` would shadow the fastapi.status module used below
    run_status: str | None = Query(None, alias="status"),
    user_id: int | None = None,
    started_after: datetime | None = None,
    started_before: datetime | None = None,
    db: AsyncSession = Depends(get_async_session),
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    _is_internal: bool = Depends(is_internal_service),
) -> list[Run]:
    """List runs with optional filters."""
    if not _is_internal and x_telegram_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    query = select(Run)

    # Apply filters
    if project_id:
        query = query.where(Run.project_id == project_id)
    if task_id:
        query = query.where(Run.task_id == task_id)
    if story_id:
        query = query.where(Run.story_id == story_id)
    if run_type:
        query = query.where(Run.type == run_type)
    if run_status:
        query = query.where(Run.status == run_status)
    if user_id is not None and _is_internal:
        query = query.where(Run.user_id == user_id)
    if started_after:
        query = query.where(Run.started_at >= started_after)
    if started_before:
        query = query.where(Run.started_at <= started_before)

    # If user provided, filter by ownership
    if x_telegram_id:
        user = await _resolve_user(x_telegram_id, db)
        if user and not user.is_admin:
            query = query.where(Run.user_id == user.id)

    # Order by creation time (newest first)
    query = query.order_by(Run.created_at.desc())

    result = await db.execute(query)
    runs = result.scalars().all()

    return list(runs)


@router.patch("/{run_id}", response_model=RunRead)
async def update_run(
    run_id: str,
    run_update: RunUpdate,
    db: AsyncSession = Depends(get_async_session),
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    _is_internal: bool = Depends(is_internal_service),
) -> Run:
    """Update run status and result."""
    query = select(Run).where(Run.id == run_id)
    result = await db.execute(query)
    run = result.scalar_one_or_none()

    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )

    # Only internal services or admins can update runs
    if not _is_internal:
        if x_telegram_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
            )
        user = await _resolve_user(x_telegram_id, db)
        if not user or not user.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only system and admins can update runs",
            )

    # Update fields
    update_data = run_update.model_dump(exclude_unset=True)

    # A terminal run has produced its outcome and nothing may start work for it
    # again. Letting a blind write take it back to QUEUED or RUNNING is how a
    # cancelled deploy comes back: whoever cancelled it acted on the cancellation
    # being final, and the resurrected run then passes every later check.
    requested_status = update_data.get("status")
    if (
        run.status in _TERMINAL_RUN_STATUSES
        and requested_status is not None
        and requested_status not in _TERMINAL_RUN_STATUSES
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run {run_id} is {run.status} and cannot go back to {requested_status}",
        )

    # A terminal run that already carries a result has said what happened, and
    # that answer is what everything downstream reads. Refusing only the move
    # back to a live status is not enough: terminal-to-terminal is the same
    # overwrite. Two writers race for one run whenever a supervisor ends a run
    # the worker is still inside — a QA run failed for losing its temporary
    # access, say — and the worker's later "passed" would replace the named
    # failure with a success the story then publishes.
    #
    # The result may still be filled in on a terminal run that has none. That is
    # not a second outcome, it is the first one: a cancelled deploy is marked
    # terminal by whoever cancelled it and the worker that owned it records what
    # it actually did afterwards, which is the only proof its dispatch is over.
    #
    # A writer repeating its own answer after a lost response is not racing
    # anybody, so an identical write is accepted as the no-op it is.
    if run.status in _TERMINAL_RUN_STATUSES and run.result is not None:
        rewritten = [
            field
            for field in _OUTCOME_FIELDS
            if field in update_data and update_data[field] != getattr(run, field)
        ]
        if rewritten:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Run {run_id} is {run.status} and has recorded its outcome; "
                    f"cannot rewrite {', '.join(rewritten)}"
                ),
            )

    for field, value in update_data.items():
        if field == "run_metadata" and value is not None:
            # Merge metadata instead of replacing to preserve existing keys.
            # A fresh dict is required: run_metadata is a plain JSON column,
            # so in-place mutation does not mark the attribute dirty.
            run.run_metadata = {**(run.run_metadata or {}), **value}
        else:
            setattr(run, field, value)

    await db.commit()
    await db.refresh(run)

    logger.info(
        "run_updated",
        run_id=run.id,
        status=run.status,
        updated_fields=list(update_data.keys()),
    )

    return run


async def _lock_run(run_id: str, db: AsyncSession) -> Run:
    """Read a run with its row locked for the length of this transaction.

    Claiming the dispatch boundary and withdrawing it are the same decision seen
    from two sides, and both are taken by different processes at the same moment.
    Read-then-write without the lock would let both sides read "not claimed yet"
    and both act.
    """
    result = await db.execute(select(Run).where(Run.id == run_id).with_for_update())
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )
    return run


def _claimed_at(run: Run) -> datetime | None:
    stamp = (run.run_metadata or {}).get(DISPATCH_CLAIMED_AT_KEY)
    return datetime.fromisoformat(stamp) if stamp else None


def _lease_expires_at(run: Run) -> datetime | None:
    stamp = (run.run_metadata or {}).get(DISPATCH_LEASE_EXPIRES_AT_KEY)
    return datetime.fromisoformat(stamp) if stamp else None


@router.post("/{run_id}/start", response_model=DeployRunStart)
async def start_run(
    run_id: str,
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(require_internal_or_admin),
) -> DeployRunStart:
    """Take a run to RUNNING, or report that it is already over.

    A worker picking a message up reads the run first and then marks it running.
    Between those two calls a withdrawal can cancel it, and the plain write puts
    the cancellation back into a state every later check accepts — including the
    dispatch claim. Read and write are one locked decision here, so a run
    cancelled at any moment before this call stays cancelled and the worker is
    told to stop instead of starting.

    Starting a run that is already running is the same answer, so a worker
    retrying after a lost response is not refused its own start.
    """
    run = await _lock_run(run_id, db)
    if run.status in _TERMINAL_RUN_STATUSES:
        await db.commit()
        logger.info("run_start_refused", run_id=run_id, run_status=run.status)
        return DeployRunStart(run_id=run_id, started=False, run_status=RunStatus(run.status))

    run.status = RunStatus.RUNNING.value
    await db.commit()
    return DeployRunStart(run_id=run_id, started=True, run_status=RunStatus.RUNNING)


@router.post("/{run_id}/dispatch-claim", response_model=DeployDispatchClaim)
async def claim_run_dispatch(
    run_id: str,
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(require_internal_or_admin),
) -> DeployDispatchClaim:
    """Take the dispatch boundary for a run that is about to reach GitHub.

    A cancelled run is refused, and the refusal is what keeps it from crossing:
    the worker asks immediately before it dispatches, so a cancellation that
    landed at any point up to here stops the deploy instead of racing it.
    Claiming again is the same answer, so a retry after a lost response is safe.

    The answer carries a deadline. Holding the boundary open indefinitely is
    what left a dead worker's grant unrevokable: nothing outside could tell a
    worker that is about to dispatch from one that never will. The lease is the
    holder's promise not to dispatch after it, renewed each time it asks, and it
    is what lets reconciliation take a silent claim back rather than wait for a
    process that is gone.
    """
    run = await _lock_run(run_id, db)
    if run.status in _TERMINAL_RUN_STATUSES:
        await db.commit()
        logger.info("run_dispatch_claim_refused", run_id=run_id, run_status=run.status)
        return DeployDispatchClaim(
            run_id=run_id,
            granted=False,
            run_status=RunStatus(run.status),
            claimed_at=_claimed_at(run),
            lease_expires_at=_lease_expires_at(run),
        )

    now = datetime.now(UTC)
    claimed_at = _claimed_at(run) or now
    lease_expires_at = now + DISPATCH_LEASE
    run.run_metadata = {
        **(run.run_metadata or {}),
        DISPATCH_CLAIMED_AT_KEY: claimed_at.isoformat(),
        DISPATCH_LEASE_EXPIRES_AT_KEY: lease_expires_at.isoformat(),
    }
    await db.commit()
    logger.info(
        "run_dispatch_claimed",
        run_id=run_id,
        claimed_at=claimed_at.isoformat(),
        lease_expires_at=lease_expires_at.isoformat(),
    )
    return DeployDispatchClaim(
        run_id=run_id,
        granted=True,
        run_status=RunStatus(run.status),
        claimed_at=claimed_at,
        lease_expires_at=lease_expires_at,
    )


@router.post("/{run_id}/dispatch-supersede", response_model=DeployDispatchSupersede)
async def supersede_run_dispatch(
    run_id: str,
    reason: str = Query("", description="Recorded on the run as error_message"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(require_internal_or_admin),
) -> DeployDispatchSupersede:
    """Take back a dispatch claim whose holder went quiet, once it may no longer act.

    A worker that claimed the boundary and then died leaves a run that never
    reaches a terminal result. Whatever is waiting on that run to know what
    happened outside — a temporary access grant waiting to be revoked, above all
    — would wait for good, and an alert about it is not a removal.

    So the claim expires. Until the lease runs out the claimer may still be on
    its way to GitHub and the only honest answer is to wait. After it, the
    claimer has promised not to dispatch, and this closes the boundary against
    it under the same lock the claim was taken under: the run is cancelled, so a
    re-claim is refused, and the crossing is stamped as superseded, which is the
    caller's proof that nothing more can appear outside without being visible on
    GitHub Actions where a fence can reach it.

    The claimer's own result is not written here. If it is alive after all it
    still records what it did, and that is the account of the deploy; this only
    records that the wait for it is over.
    """
    run = await _lock_run(run_id, db)
    claimed_at = _claimed_at(run)
    lease_expires_at = _lease_expires_at(run)

    def _answer(outcome: DispatchSupersede) -> DeployDispatchSupersede:
        return DeployDispatchSupersede(
            run_id=run_id,
            outcome=outcome,
            run_status=RunStatus(run.status),
            claimed_at=claimed_at,
            lease_expires_at=lease_expires_at,
        )

    if run.status in _TERMINAL_RUN_STATUSES and run.result is not None:
        await db.commit()
        return _answer(DispatchSupersede.ALREADY_SETTLED)
    if claimed_at is None:
        await db.commit()
        return _answer(DispatchSupersede.NOT_CLAIMED)
    if (run.run_metadata or {}).get(DISPATCH_SUPERSEDED_AT_KEY):
        await db.commit()
        return _answer(DispatchSupersede.SUPERSEDED)
    if lease_expires_at is not None and lease_expires_at > datetime.now(UTC):
        await db.commit()
        logger.info(
            "run_dispatch_supersede_deferred",
            run_id=run_id,
            lease_expires_at=lease_expires_at.isoformat(),
        )
        return _answer(DispatchSupersede.LEASE_LIVE)

    run.status = RunStatus.CANCELLED.value
    if reason:
        run.error_message = reason
    run.run_metadata = {
        **(run.run_metadata or {}),
        DISPATCH_SUPERSEDED_AT_KEY: datetime.now(UTC).isoformat(),
    }
    await db.commit()
    logger.warning(
        "run_dispatch_superseded",
        run_id=run_id,
        claimed_at=claimed_at.isoformat(),
        reason=reason,
    )
    return _answer(DispatchSupersede.SUPERSEDED)


@router.post("/{run_id}/dispatch-withdraw", response_model=DeployDispatchWithdrawal)
async def withdraw_run_dispatch(
    run_id: str,
    reason: str = Query("", description="Recorded on the run as error_message"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(require_internal_or_admin),
) -> DeployDispatchWithdrawal:
    """Stop a deploy run, and say whether it was stopped before it reached GitHub.

    Cancelling is not enough on its own to know what the caller is dealing with.
    An unclaimed run is withdrawn outright and nothing of it exists outside. A
    claimed one is still marked cancelled — the worker polls that and stops its
    own Actions run — but the caller is told the deploy is already outside, so it
    waits for the run to end rather than acting as if nothing was dispatched.
    """
    run = await _lock_run(run_id, db)
    claimed_at = _claimed_at(run)

    if run.status in _TERMINAL_RUN_STATUSES:
        await db.commit()
        return DeployDispatchWithdrawal(
            run_id=run_id,
            outcome=DispatchWithdrawal.ALREADY_TERMINAL,
            run_status=RunStatus(run.status),
            claimed_at=claimed_at,
        )

    run.status = RunStatus.CANCELLED.value
    if reason:
        run.error_message = reason
    await db.commit()
    outcome = DispatchWithdrawal.ALREADY_DISPATCHED if claimed_at else DispatchWithdrawal.WITHDRAWN
    logger.info("run_dispatch_withdrawn", run_id=run_id, outcome=outcome.value)
    return DeployDispatchWithdrawal(
        run_id=run_id,
        outcome=outcome,
        run_status=RunStatus.CANCELLED,
        claimed_at=claimed_at,
    )

"""Runs router (execution layer)."""

from datetime import UTC, datetime
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.bot_rollout import (
    BOT_ROLLOUT_METADATA_KEY,
    BOT_ROLLOUT_NOTIFY_KEY,
    BotRolloutNotifyState,
    BotRolloutPublishState,
)
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
from shared.contracts.dto.engineering_attempt import EngineeringAttemptLedgerInput
from shared.contracts.dto.executor_decision import EXECUTOR_DECISION_METADATA_KEY
from shared.contracts.dto.owner_notification import (
    OWNER_NOTIFICATION_KEY,
    OwnerNotificationState,
)
from shared.contracts.dto.qa_ssh_grant import QA_SSH_GRANT_KEY, QASshGrantState
from shared.contracts.dto.run import RunStatus, RunType
from shared.models import EngineeringAttemptLedger, Project, Run, User

from ..database import get_async_session
from ..dependencies import (
    _optional_bearer_scheme,
    is_internal_service,
    require_internal_or_admin,
    resolve_actor,
)
from ..engineering_budget_admission import finalize_engineering_reservation
from ..schemas import RunCreate, RunRead, RunUpdate

logger = structlog.get_logger()

router = APIRouter(prefix="/runs", tags=["runs"])

# A run in one of these has produced its outcome; nothing may start work for it.
_TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}
)

# What a run says happened. The first recorded outcome owns these fields; QA
# cancellation is an outcome even without a typed result.
_OUTCOME_FIELDS = ("status", "result", "error_message")

# Largest page the QA grant selection will hand out at once. The page bounds one
# response, never the coverage: the caller walks pages from a cursor until one
# comes back short, so no unreleased record falls off the end of the selection.
QA_SSH_GRANT_PAGE_MAX = 500

# Largest page of runs owing an owner notification. The selection drains by
# itself — every attempt either delivers the message or spends one of a bounded
# number of tries — so a page is all the bound the recovery sweep needs.
OWNER_NOTIFICATION_PAGE_MAX = 500

# Ledger rows are immutable history, but an unrestricted historical query can
# still exhaust the API process. Keep this aligned with the router's bounded
# selection endpoints.
ENGINEERING_ATTEMPT_PAGE_MAX = 500


class EngineeringAttemptRead(BaseModel):
    """Read-only representation of the canonical engineering ledger."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    idempotency_key: str
    run_id: str | None
    project_id: uuid.UUID | None
    story_id: str | None
    task_id: str | None
    user_id: int | None
    owner_attribution: str
    role: str
    occurred_at: datetime
    provider: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    cost_microusd: int | None
    cost_source: str


def _engineering_attempt_key(run_id: str) -> str:
    """Stable identity for the one engineering coding attempt owned by a Run."""
    return f"engineering-run:{run_id}"


async def _attach_ledger_compatibility(runs: list[Run], db: AsyncSession) -> None:
    """Expose old Run observability fields as projections of the ledger.

    The database columns remain for rolling compatibility only. New engineering
    writes never populate them; API responses prefer the canonical ledger row.
    """
    if not runs:
        return
    rows = (
        await db.execute(
            select(EngineeringAttemptLedger).where(
                EngineeringAttemptLedger.run_id.in_([run.id for run in runs])
            )
        )
    ).scalars()
    attempts = {attempt.run_id: attempt for attempt in rows}
    for run in runs:
        attempt = attempts.get(run.id)
        if attempt is None:
            continue
        run._ledger_input_tokens = attempt.input_tokens
        run._ledger_output_tokens = attempt.output_tokens
        run._ledger_total_tokens = attempt.total_tokens
        run._ledger_cost_usd = (
            attempt.cost_microusd / 1_000_000 if attempt.cost_microusd is not None else None
        )


async def _record_engineering_attempt(
    run: Run,
    attempt: EngineeringAttemptLedgerInput | None,
    db: AsyncSession,
) -> None:
    """Append the one ledger record while the terminal Run row is locked.

    A retry sees the row written by the first delivery and performs no mutable
    accounting write. The unique run/idempotency constraints remain the final
    fence if a writer ever bypasses this seam.
    """
    existing = await db.scalar(
        select(EngineeringAttemptLedger.id).where(EngineeringAttemptLedger.run_id == run.id)
    )
    if existing is not None:
        return
    facts = attempt or EngineeringAttemptLedgerInput()
    project = await db.get(Project, run.project_id) if run.project_id is not None else None
    db.add(
        EngineeringAttemptLedger(
            idempotency_key=_engineering_attempt_key(run.id),
            run_id=run.id,
            project_id=run.project_id,
            story_id=run.story_id,
            task_id=run.task_id,
            user_id=project.owner_id if project is not None else None,
            owner_attribution="resolved" if project is not None else "unknown",
            occurred_at=run.completed_at or datetime.now(UTC),
            provider=facts.provider,
            model=facts.model,
            input_tokens=facts.input_tokens,
            output_tokens=facts.output_tokens,
            total_tokens=facts.total_tokens,
            cache_read_tokens=facts.cache_read_tokens,
            cache_write_tokens=facts.cache_write_tokens,
            cost_microusd=facts.cost_microusd,
            cost_source=facts.cost_source.value,
        )
    )


async def _check_run_access(
    run: Run,
    telegram_id: int | None,
    db: AsyncSession,
    *,
    is_internal: bool = False,
    credentials: HTTPAuthorizationCredentials | None,
) -> None:
    """Check if the request may reach this run. Raises 401/403/404 if denied.

    Who is acting is `resolve_actor`'s decision, not this function's — the same
    decision the project guard asks for. A run id travels in a user's message, so
    a request that names a user is judged as that user however it was
    authenticated; a service acting for itself passes.
    """
    actor = await resolve_actor(
        is_internal=is_internal,
        telegram_id=telegram_id,
        credentials=credentials,
        db=db,
    )

    if actor is None or actor.is_admin:
        return

    # Regular user: must be owner
    if run.user_id != actor.id:
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
    if run.type in (RunType.ENGINEERING.value, RunType.QA.value):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Paid coding-agent runs must use the paid-run start command",
        )
    run_data = run.model_dump()
    if run.project_id is not None:
        project = await db.get(Project, run.project_id)
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        # Project ownership is canonical. A caller cannot stamp a project-bound
        # run with another user, whether accidentally or maliciously.
        run_data["user_id"] = project.owner_id

    # Verify user exists if user_id provided
    if run_data["user_id"]:
        query = select(User).where(User.id == run_data["user_id"])
        result = await db.execute(query)
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with id {run_data['user_id']} not found",
            )

    db_run = Run(**run_data)
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


@router.get("/engineering-attempts", response_model=list[EngineeringAttemptRead])
async def list_engineering_attempts(  # noqa: PLR0913
    user_id: int | None = None,
    project_id: uuid.UUID | None = None,
    story_id: str | None = None,
    task_id: str | None = None,
    run_id: str | None = None,
    occurred_after: datetime | None = None,
    occurred_before: datetime | None = None,
    limit: int = Query(100, ge=1, le=ENGINEERING_ATTEMPT_PAGE_MAX),
    db: AsyncSession = Depends(get_async_session),
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> list[EngineeringAttemptLedger]:
    """List canonical engineering-attempt ledger rows. This router has no writer."""
    actor = await resolve_actor(
        is_internal=_is_internal,
        telegram_id=x_telegram_id,
        credentials=credentials,
        db=db,
    )
    if run_id is not None and actor is not None and not actor.is_admin:
        named_owner = await db.scalar(
            select(EngineeringAttemptLedger.user_id).where(
                EngineeringAttemptLedger.run_id == run_id
            )
        )
        if named_owner is not None and named_owner != actor.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: not engineering attempt owner",
            )
    query = select(EngineeringAttemptLedger)
    if project_id is not None:
        query = query.where(EngineeringAttemptLedger.project_id == project_id)
    if story_id is not None:
        query = query.where(EngineeringAttemptLedger.story_id == story_id)
    if task_id is not None:
        query = query.where(EngineeringAttemptLedger.task_id == task_id)
    if run_id is not None:
        query = query.where(EngineeringAttemptLedger.run_id == run_id)
    if occurred_after is not None:
        query = query.where(EngineeringAttemptLedger.occurred_at >= occurred_after)
    if occurred_before is not None:
        query = query.where(EngineeringAttemptLedger.occurred_at <= occurred_before)
    if actor is not None and not actor.is_admin:
        query = query.where(EngineeringAttemptLedger.user_id == actor.id)
    elif user_id is not None:
        query = query.where(EngineeringAttemptLedger.user_id == user_id)
    result = await db.execute(
        query.order_by(EngineeringAttemptLedger.occurred_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


@router.get("/{run_id}", response_model=RunRead)
async def get_run(
    run_id: str,
    db: AsyncSession = Depends(get_async_session),
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
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

    await _check_run_access(
        run,
        x_telegram_id,
        db,
        is_internal=_is_internal,
        credentials=credentials,
    )
    await _attach_ledger_compatibility([run], db)
    return run


@router.get("/", response_model=list[RunRead])
async def list_runs(  # noqa: PLR0913
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
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> list[Run]:
    """List runs with optional filters."""
    # Resolved before any filter is applied: naming a user_id must not be a way to
    # read another user's runs, and neither must holding the internal key.
    actor = await resolve_actor(
        is_internal=_is_internal,
        telegram_id=x_telegram_id,
        credentials=credentials,
        db=db,
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
    if user_id is not None and actor is None:
        query = query.where(Run.user_id == user_id)
    if started_after:
        query = query.where(Run.started_at >= started_after)
    if started_before:
        query = query.where(Run.started_at <= started_before)

    # A named user sees only their own runs; an admin sees all of them.
    if actor is not None and not actor.is_admin:
        query = query.where(Run.user_id == actor.id)

    # Order by creation time (newest first)
    query = query.order_by(Run.created_at.desc())

    result = await db.execute(query)
    runs = result.scalars().all()
    await _attach_ledger_compatibility(list(runs), db)
    return list(runs)


@router.get("/qa-ssh-grants/held", response_model=list[RunRead])
async def list_runs_holding_qa_ssh_grants(
    limit: int = Query(100, ge=1, le=QA_SSH_GRANT_PAGE_MAX),
    after_created_at: datetime | None = Query(None),
    after_id: str | None = Query(None),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(require_internal_or_admin),
) -> list[Run]:
    """Every run whose QA SSH grant is not proven released, oldest first.

    The QA grant sweep needs its work selected by the state of the record, not
    by when the run started. Asking `/runs/` for a recent window was the wrong
    key: an outage longer than the window put a live `authorized_keys` line
    permanently out of reach of the only process that removes it. A record is
    work while it is unreleased, whether that became true a minute ago or a
    month ago.

    So age is neither a reason to skip a record nor a reason to close it. What
    bounds the answer is the page, and the order is oldest first so a caller
    walking pages drains the whole selection rather than a recent slice of it.

    The page is taken from a cursor — strictly after `(created_at, id)` of the
    last record the caller handled — and never from an offset. The selection
    shrinks while it is being walked, because handling a record is what
    releases it; under an offset that shrinking moves unhandled records
    backwards past the cursor and a walk can end while open records remain. A
    keyset cursor names a position in the order rather than a count of rows, so
    rows leaving the selection behind it move nothing ahead of it.

    A record with no readable state is deliberately still selected: it is a
    malformed grant, and the caller must fail on it loudly rather than never
    see it.
    """
    if (after_created_at is None) != (after_id is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="after_created_at and after_id name one cursor and must be given together",
        )

    grant = Run.run_metadata[QA_SSH_GRANT_KEY]
    grant_state = Run.run_metadata[(QA_SSH_GRANT_KEY, "state")].as_string()
    query = select(Run).where(
        grant.is_not(None),
        grant_state.is_distinct_from(QASshGrantState.RELEASED.value),
    )
    if after_created_at is not None:
        query = query.where(tuple_(Run.created_at, Run.id) > tuple_(after_created_at, after_id))
    query = query.order_by(Run.created_at.asc(), Run.id.asc()).limit(limit)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.get("/owner-notifications/owed", response_model=list[RunRead])
async def list_runs_owing_owner_notification(
    limit: int = Query(100, ge=1, le=OWNER_NOTIFICATION_PAGE_MAX),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(require_internal_or_admin),
) -> list[Run]:
    """Every run whose owner has not been told its story ended, oldest first.

    The supervisor writes this record before it commits a terminal transition,
    which is what makes the message recoverable at all: the moment the story
    leaves TESTING it is invisible to the loop that routed it, so the work has
    to be selected by the state of the record instead of by the story's status.

    Age is not part of the selection. A record is work while it is owed, whether
    the publish behind it failed a minute ago or during an outage last week —
    the same reason the QA grant selection above dropped its time window. What
    bounds the answer is the page, and the order is oldest first so the owner
    who has been waiting longest is served first.

    No cursor is needed here, unlike the grant selection: every visit to a
    record either delivers it or spends one of its bounded attempts, so a record
    cannot stay in the selection across more ticks than that bound, and the head
    of the page cannot wedge behind it.
    """
    notification = Run.run_metadata[OWNER_NOTIFICATION_KEY]
    state = Run.run_metadata[(OWNER_NOTIFICATION_KEY, "state")].as_string()
    query = (
        select(Run)
        .where(notification.is_not(None), state == OwnerNotificationState.OWED.value)
        .order_by(Run.created_at.asc(), Run.id.asc())
        .limit(limit)
    )
    result = await db.execute(query)
    return list(result.scalars().all())


@router.get("/bot-rollouts/unsettled", response_model=list[RunRead])
async def list_unsettled_bot_rollout_runs(
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(require_internal_or_admin),
) -> list[Run]:
    """Every configuration-only rollout whose publish or notify is unsettled.

    Selected by the state of the records, oldest first, for the same reason as
    the owner-notification selection above: the commit/publish gap is closed by
    re-attempting from a durable record, not from a time window. A run qualifies
    while its rollout record says the queue write is still owed (bounded
    attempts, then an admin alert) or its notify record says the owner has not
    heard the ending. Both settle by transition, so no record can stay in the
    selection across more ticks than its bound.
    """
    rollout = Run.run_metadata[BOT_ROLLOUT_METADATA_KEY]
    publish_state = Run.run_metadata[(BOT_ROLLOUT_METADATA_KEY, "publish")].as_string()
    notify = Run.run_metadata[BOT_ROLLOUT_NOTIFY_KEY]
    notify_state = Run.run_metadata[(BOT_ROLLOUT_NOTIFY_KEY, "state")].as_string()
    query = (
        select(Run)
        .where(
            # A deploy run carrying rollout bookkeeping...
            rollout.is_not(None),
            or_(
                # ...whose message may never have reached the queue, or
                #
                # An ABANDONED record deliberately does not qualify: its
                # bounded attempts ran out, a human was alerted, and selecting
                # it forever would spend a sweep page on runs nothing may
                # touch again.
                publish_state == BotRolloutPublishState.PUBLISH_OWED.value,
                # ...whose owner is still owed the terminal outcome.
                and_(notify.is_not(None), notify_state == BotRolloutNotifyState.OWED.value),
            ),
        )
        .order_by(Run.created_at.asc(), Run.id.asc())
        .limit(limit)
    )
    result = await db.execute(query)
    return list(result.scalars().all())


async def _lock_run(run_id: str, db: AsyncSession) -> Run:
    """Read a run with its row locked for the length of this transaction.

    Every writer that decides something from what the run already says takes it
    this way. Claiming the dispatch boundary and withdrawing it are the same
    decision seen from two sides, taken by different processes at the same
    moment; so are a QA worker's verdict and the sweep's named access failure.
    Read-then-write without the lock lets both sides read the state before either
    acted, and both act.
    """
    result = await db.execute(select(Run).where(Run.id == run_id).with_for_update())
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )
    return run


def _record_first_terminal_completion(run: Run) -> None:
    """Stamp a terminal run once, in the transaction that records its outcome."""
    if run.completed_at is None:
        run.completed_at = datetime.now(UTC)


def _has_recorded_outcome(run: Run) -> bool:
    """Whether a terminal row already owns its immutable outcome fields.

    QA cancellation is itself a final verdict: a central QA worker returning
    later must not replace it. Deploy cancellation is a dispatch boundary and
    intentionally carries no typed result; its worker may still be alive and
    must be allowed to record the first account of what happened outside.
    """
    return run.result is not None or (
        run.type in {RunType.QA.value, RunType.ENGINEERING.value}
        and run.status in _TERMINAL_RUN_STATUSES
    )


@router.patch("/{run_id}", response_model=RunRead)
async def update_run(
    run_id: str,
    run_update: RunUpdate,
    db: AsyncSession = Depends(get_async_session),
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> Run:
    """Update run status and result.

    The row is locked before it is read. The outcome rules below are decided from
    what the run says now, and a plain read makes that decision against a value
    another transaction is already replacing: a QA worker's pass and the sweep's
    cleanup failure both read a running run, both pass the guard, and whichever
    commits last is what the story reads. Reading under the lock makes the two
    writers take turns, so the second one sees the first one's answer and is
    refused by the same rules that exist to refuse it.
    """
    run = await _lock_run(run_id, db)

    # Only services acting for themselves, and admins, can update runs
    actor = await resolve_actor(
        is_internal=_is_internal,
        telegram_id=x_telegram_id,
        credentials=credentials,
        db=db,
    )
    if actor is not None and not actor.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only system and admins can update runs",
        )

    # Update fields. The API owns completed_at, not callers: a non-terminal
    # update cannot pre-seed a timestamp for a later terminal transition.
    update_data = run_update.model_dump(exclude_unset=True)
    update_data.pop("completed_at", None)
    if "user_id" in update_data and run.project_id is not None:
        project = await db.get(Project, run.project_id)
        if project is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Run {run_id} has no persisted project owner",
            )
        if update_data["user_id"] != project.owner_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Project-bound runs must retain their persisted project owner",
            )
    requested_status = update_data.get("status")
    engineering_attempt = run_update.engineering_attempt
    update_data.pop("engineering_attempt", None)
    if engineering_attempt is not None and run.type != RunType.ENGINEERING.value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="engineering_attempt is only valid for engineering runs",
        )
    if engineering_attempt is not None and requested_status not in _TERMINAL_RUN_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="engineering_attempt is only valid with a terminal engineering status",
        )

    # A terminal run has produced its outcome and nothing may start work for it
    # again. Letting a blind write take it back to QUEUED or RUNNING is how a
    # cancelled deploy comes back: whoever cancelled it acted on the cancellation
    # being final, and the resurrected run then passes every later check.
    if (
        run.status in _TERMINAL_RUN_STATUSES
        and requested_status is not None
        and requested_status not in _TERMINAL_RUN_STATUSES
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run {run_id} is {run.status} and cannot go back to {requested_status}",
        )

    # The API owns a run's terminal timestamp. It is written in the same
    # transaction as the first terminal state/result, and subsequent deliveries
    # preserve it even if their payload carries a different client timestamp.
    if requested_status in _TERMINAL_RUN_STATUSES:
        _record_first_terminal_completion(run)
        update_data.pop("completed_at", None)

    # A terminal run that has recorded its outcome owns that answer. For QA,
    # cancellation is an outcome even without a typed result, so a late central
    # verdict cannot replace it. A cancelled deploy without a result is the
    # dispatch-boundary exception described in `_has_recorded_outcome`.
    #
    # A writer repeating its own answer after a lost response is not racing
    # anybody, so an identical write is accepted as the no-op it is.
    if run.status in _TERMINAL_RUN_STATUSES and _has_recorded_outcome(run):
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

    if "run_metadata" in update_data:
        metadata_update = update_data["run_metadata"]
        existing_metadata = run.run_metadata or {}
        if EXECUTOR_DECISION_METADATA_KEY in existing_metadata and (
            not isinstance(metadata_update, dict)
            or (
                EXECUTOR_DECISION_METADATA_KEY in metadata_update
                and metadata_update[EXECUTOR_DECISION_METADATA_KEY]
                != existing_metadata[EXECUTOR_DECISION_METADATA_KEY]
            )
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Run executor decision is immutable after paid-run creation",
            )

    for field, value in update_data.items():
        if field == "run_metadata" and value is not None:
            # Merge metadata instead of replacing to preserve existing keys.
            # A fresh dict is required: run_metadata is a plain JSON column,
            # so in-place mutation does not mark the attribute dirty.
            run.run_metadata = {**(run.run_metadata or {}), **value}
        else:
            setattr(run, field, value)

    # This is deliberately the only ledger writer. Every terminal engineering
    # path, including cancellation and a repeated worker delivery, uses the
    # same Run lock and transaction.
    if run.type == RunType.ENGINEERING.value and run.status in _TERMINAL_RUN_STATUSES:
        await _record_engineering_attempt(run, engineering_attempt, db)
        facts = engineering_attempt or EngineeringAttemptLedgerInput()
        await finalize_engineering_reservation(run.id, facts.cost_microusd, db)

    await db.commit()
    await db.refresh(run)
    await _attach_ledger_compatibility([run], db)

    logger.info(
        "run_updated",
        run_id=run.id,
        status=run.status,
        updated_fields=list(update_data.keys()),
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

    if run.status in _TERMINAL_RUN_STATUSES and _has_recorded_outcome(run):
        if (run.run_metadata or {}).get(DISPATCH_SUPERSEDED_AT_KEY):
            await db.commit()
            return _answer(DispatchSupersede.SUPERSEDED)
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
    _record_first_terminal_completion(run)
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
    _record_first_terminal_completion(run)
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

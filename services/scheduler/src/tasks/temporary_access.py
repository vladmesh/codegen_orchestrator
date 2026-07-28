"""Temporary access reconciliation: grant and revoke by state, not by happy path.

Access handed to a test identity is applied by a deploy of the story's commit
with the value set, and taken back by a deploy of that same commit with the
value cleared. Running the second one at the end of a successful QA run makes
revocation likely, not certain: a killed run, a cancelled one, or a dead process
between grant and revoke leaves the access standing with nobody left to remove
it.

So the grant is a durable record and this sweep is the only thing that moves it.
It reads every grant that still holds access and drives the whole lifecycle from
what it finds: it waits for the deploy that applies the value to confirm, only
then releases the QA run the access was borrowed for, and revokes as soon as
that run reaches any terminal state — or as soon as the grant outlives its
lifetime. Every step repeats until the state says it landed, which makes it safe
for the sweep itself to be interrupted at any point.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import structlog

from shared.contracts.dto.run import RunDTO, RunStatus, RunType
from shared.contracts.dto.run_result import (
    QABlocker,
    QABlockerCategory,
    QARunResult,
)
from shared.contracts.dto.temporary_access import (
    TemporaryAccessGrantCreate,
    TemporaryAccessGrantDTO,
    TemporaryAccessGrantUpdate,
    TemporaryAccessRevokeReason,
    TemporaryAccessStatus,
)
from shared.contracts.queues.deploy import (
    DeployAction,
    DeployMessage,
    DeployOutcome,
    DeployTrigger,
)
from shared.contracts.queues.qa import QAMessage, QAOutcome
from shared.notifications import notify_admins_best_effort
from shared.queues import DEPLOY_QUEUE, QA_QUEUE
from shared.redis_client import RedisStreamClient

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient

from .. import startup

logger = structlog.get_logger(__name__)

# A run in any of these states will never come back to release its grant.
TERMINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})

# Marks a grant whose deploy claimed the dispatch and then went quiet. Kept as a
# prefix on last_error so the report is made once rather than every tick.
_UNSETTLED_DISPATCH_ERROR = "grant deploy dispatch unsettled"


def _empty_counts() -> dict[str, int]:
    return {"dispatched": 0, "released": 0, "revoked": 0, "expired": 0, "revoke_failed": 0}


def _grant_ttl_minutes() -> int:
    return startup.get_config().get_int("supervisor.temporary_access_ttl_minutes")


def _revoke_stale_minutes() -> int:
    return startup.get_config().get_int("supervisor.temporary_access_revoke_stale_minutes")


def _max_revoke_attempts() -> int:
    return startup.get_config().get_int("supervisor.temporary_access_max_revoke_attempts")


def _age_minutes(moment: datetime) -> float:
    reference = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return (datetime.now(UTC) - reference).total_seconds() / 60


async def grant_temporary_access(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    *,
    project_id: str,
    env_key: str,
    subject: str,
    head_sha: str,
    qa_message: QAMessage,
) -> TemporaryAccessGrantDTO:
    """Hand the access out, record first, and hold the QA handoff.

    The record is written before the deploy that applies the value, so no order
    of crashes can produce access nobody knows about: the worst case is a record
    for access that was never applied, and revoking that is a redeploy of a value
    that is already empty.

    QA is not published here. The handoff travels on the record and the sweep
    releases it once the deploy has confirmed, so a lagging grant deploy can
    never apply the value after the access was already taken back.

    The id names the QA run, so asking twice is asking for the same grant: a
    caller repeating a handoff it could not confirm gets the first record back
    rather than a second grant competing for the same contract slot.
    """
    grant_id = f"tempaccess-{qa_message.run_id}"[:255]
    grant_run_id = _new_deploy_run_id("grant")
    grant = await api_client.create_temporary_access_grant(
        TemporaryAccessGrantCreate(
            id=grant_id,
            project_id=project_id,
            env_key=env_key,
            subject=subject,
            head_sha=head_sha,
            qa_run_id=qa_message.run_id,
            grant_run_id=grant_run_id,
            qa_message=qa_message,
        )
    )

    log = logger.bind(
        grant_id=grant.id,
        project_id=project_id,
        env_key=env_key,
        qa_run_id=grant.qa_run_id,
    )
    await _publish_grant_deploy(api_client, redis_client, grant, grant_run_id)
    log.info(
        "temporary_access_granting",
        head_sha=head_sha,
        grant_run_id=grant_run_id,
    )
    return grant


async def supervise_temporary_access(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """Settle every grant that still holds access.

    Returns counts of the actions taken this tick.
    """
    grants = await api_client.list_live_temporary_access_grants()
    counts = _empty_counts()
    if not grants:
        return counts

    for grant in grants:
        log = logger.bind(
            grant_id=grant.id,
            project_id=grant.project_id,
            env_key=grant.env_key,
            qa_run_id=grant.qa_run_id,
        )
        try:
            if grant.status is TemporaryAccessStatus.GRANTING:
                await _settle_grant_in_flight(api_client, redis_client, grant, counts, log)
            elif grant.status is TemporaryAccessStatus.REVOKING:
                await _settle_revoke_in_flight(api_client, redis_client, grant, counts, log)
            else:
                await _settle_granted(api_client, redis_client, grant, counts, log)
        except Exception:
            # One grant that cannot be settled is that grant's problem. It stays
            # live and is retried next tick; the other grants still get swept.
            log.exception("temporary_access_grant_sweep_error", status=grant.status.value)
            counts["revoke_failed"] += 1

    return counts


def _new_deploy_run_id(kind: str) -> str:
    return f"deploy-{kind}-{uuid.uuid4().hex[:8]}"


async def _publish_deploy(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    *,
    grant: TemporaryAccessGrantDTO,
    run_id: str,
    value: str,
    run_metadata: dict,
    fence_active_deploys: bool,
) -> None:
    """Deploy the granted commit with *value* in the grant's contract slot.

    Same commit, same bot, one value changed, so the identity gains or loses
    access on the application it was tested against rather than on whatever the
    branch has become since.
    """
    await api_client.create_run(
        {
            "id": run_id,
            "type": RunType.DEPLOY.value,
            "project_id": grant.project_id,
            "status": RunStatus.QUEUED.value,
            "run_metadata": {"head_sha": grant.head_sha, "grant_id": grant.id, **run_metadata},
        }
    )
    await redis_client.publish_message(
        DEPLOY_QUEUE,
        DeployMessage(
            task_id=run_id,
            project_id=grant.project_id,
            user_id="",
            story_id="",
            triggered_by=DeployTrigger.ADMIN,
            action=DeployAction.FEATURE,
            head_sha=grant.head_sha,
            env_overrides={grant.env_key: value},
            fence_active_deploys=fence_active_deploys,
        ),
    )


async def _publish_grant_deploy(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    grant: TemporaryAccessGrantDTO,
    grant_run_id: str,
) -> None:
    await _publish_deploy(
        api_client,
        redis_client,
        grant=grant,
        run_id=grant_run_id,
        value=grant.subject,
        run_metadata={"triggered_by": "temporary_access_grant"},
        fence_active_deploys=False,
    )


async def _settle_grant_in_flight(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    grant: TemporaryAccessGrantDTO,
    counts: dict[str, int],
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Read the deploy that applies the access and confirm, retry, or give up.

    Nothing may revoke while this deploy is still in flight: a revoke that
    overtook it would clear a value the deploy then writes back, and the record
    would be settled while the access stands. So a QA run that ended early waits
    here until the grant deploy has answered.
    """
    if _age_minutes(grant.granted_at) >= _grant_ttl_minutes():
        await _abandon_grant(
            api_client,
            redis_client,
            grant,
            f"grant deploy {grant.grant_run_id} did not confirm within "
            f"{_grant_ttl_minutes()} minutes",
            counts,
            log,
        )
        return

    run = await api_client.get_run_if_missing_returns_none(grant.grant_run_id)
    if run is None:
        # The record survived a process that died before (or while) publishing.
        # The access was intended and may never have been applied, so ask again.
        log.warning("temporary_access_grant_run_missing", grant_run_id=grant.grant_run_id)
        await _redispatch_grant(api_client, redis_client, grant, counts, log)
        return

    if run.status not in TERMINAL_RUN_STATUSES:
        if _age_minutes(run.created_at) < _revoke_stale_minutes():
            return
        await _abandon_grant(
            api_client,
            redis_client,
            grant,
            f"grant deploy {run.id} is still {run.status.value} after "
            f"{_revoke_stale_minutes()} minutes",
            counts,
            log,
        )
        return

    if run.status is RunStatus.CANCELLED:
        # Losing the project's deploy lock is contention, not refusal.
        log.info("temporary_access_grant_superseded", grant_run_id=run.id)
        await _redispatch_grant(api_client, redis_client, grant, counts, log)
        return

    if not _deploy_succeeded(run):
        await _abandon_grant(
            api_client, redis_client, grant, _deploy_failure_detail(run, "grant"), counts, log
        )
        return

    await api_client.update_temporary_access_grant(
        grant.id, TemporaryAccessGrantUpdate(status=TemporaryAccessStatus.GRANTED)
    )
    log.info("temporary_access_granted", grant_run_id=run.id, head_sha=grant.head_sha)
    await _settle_granted(
        api_client,
        redis_client,
        grant.model_copy(update={"status": TemporaryAccessStatus.GRANTED}),
        counts,
        log,
    )


async def _redispatch_grant(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    grant: TemporaryAccessGrantDTO,
    counts: dict[str, int],
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Ask for the grant deploy again under a new run id.

    The record moves to the new id before the message is published, so an
    interrupted publish is a grant deploy that never started rather than a run
    id nothing is watching. The grant's own lifetime bounds the repetition.
    """
    grant_run_id = _new_deploy_run_id("grant")
    await api_client.update_temporary_access_grant(
        grant.id, TemporaryAccessGrantUpdate(grant_run_id=grant_run_id)
    )
    await _publish_grant_deploy(api_client, redis_client, grant, grant_run_id)
    log.info("temporary_access_grant_redispatched", grant_run_id=grant_run_id)
    counts["dispatched"] += 1


async def _abandon_grant(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    grant: TemporaryAccessGrantDTO,
    detail: str,
    counts: dict[str, int],
    log: structlog.stdlib.BoundLogger,
) -> None:
    """The deploy that was to hand the access out never confirmed it.

    Whether the value reached the application is exactly what is unknown, so the
    slot is cleared either way and the QA run that was waiting for the access
    fails with the reason named instead of starting without it.

    The abandoned deploy may still be live on GitHub Actions — that is what
    "unconfirmed" means here. The revoke this dispatches carries the fence, so it
    stops that run before writing rather than racing it. A grant deploy that no
    consumer has picked up yet is not on Actions and no fence can reach it, so
    its run is withdrawn first: a deploy consumer refuses a run that was
    cancelled before it started.

    A withdrawal that arrives after the worker claimed the dispatch is the one
    case where neither mechanism can settle it now: the Actions run may not exist
    yet, so the fence cannot see it, and the worker is on its way to creating it.
    Nothing is revoked on that tick. The worker reads the cancellation, stops its
    own run and records its outcome, and the next sweep revokes against a state
    that can be fenced.

    The QA run is failed before any of that. It is not waiting for the access any
    more whatever the grant deploy turns out to have done, and a grant deploy
    that never settles must not leave the run with no named reason.
    """
    log.error("temporary_access_grant_failed", detail=detail)
    await _fail_qa_run(
        api_client,
        grant,
        category=QABlockerCategory.QA_ACCESS_GRANT_FAILED,
        attempted=f"grant temporary access {grant.env_key} for project {grant.project_id}",
        sent=f"deploy of {grant.head_sha} with {grant.env_key} set",
        received=detail,
        summary="temporary test access could not be granted",
        error_message=f"temporary access {grant.env_key} was never confirmed: {detail}",
        log=log,
    )
    if not await _stop_grant_deploy(api_client, grant, detail, log):
        return
    await _dispatch_revoke(
        api_client, redis_client, grant, TemporaryAccessRevokeReason.GRANT_FAILED, log
    )
    counts["dispatched"] += 1


async def _stop_grant_deploy(
    api_client: SchedulerAPIClient,
    grant: TemporaryAccessGrantDTO,
    detail: str,
    log: structlog.stdlib.BoundLogger,
) -> bool:
    """Withdraw the grant deploy before anything clears the value it would set.

    Cancelling the GitHub Actions run is the revoke deploy's fence, and it only
    reaches a deploy that already started. Between the queue and that run there
    are two states no fence covers: a message no consumer has picked up, and a
    worker that has decided to dispatch but has not reached GitHub yet. Both
    would write the identity back onto a grant recorded as revoked.

    Withdrawing settles the first outright and answers the second honestly. The
    withdrawal and the worker's claim are decided against the same locked row, so
    a deploy that has not crossed the boundary never will, and one that has is
    reported as already outside.

    A deploy that did cross keeps the revoke waiting until the worker holding it
    says what it did. Time cannot stand in for that. Elapsed seconds prove only
    that a claim is old, not that the GitHub run it was about to make exists to
    be fenced: a worker paused past the wait, or one whose claim response arrived
    late, still reaches ``workflow_dispatch`` afterwards, and the value goes back
    on an application whose grant is already recorded revoked. So the only thing
    accepted as proof is the claimer's own recorded outcome, which it writes on
    every path it can leave the deploy by. Until then this grant is not
    revocable, and a claim that stays unanswered too long is reported rather than
    waited out.

    Returns True when the revoke may proceed, False while the grant deploy is
    still on its way out and has to be waited for.
    """
    run = await api_client.get_run_if_missing_returns_none(grant.grant_run_id)
    if run is None:
        return True
    if _dispatch_settled(run):
        return True

    withdrawal = await api_client.withdraw_deploy_dispatch(
        grant.grant_run_id,
        f"temporary access grant {grant.id} was abandoned: {detail}",
    )
    if withdrawal.claimed_at is None:
        log.info(
            "temporary_access_grant_deploy_cancelled",
            grant_run_id=grant.grant_run_id,
            outcome=withdrawal.outcome.value,
        )
        return True

    # Claimed. The withdrawal cancelled the run, so the worker will stop, but
    # only it knows whether it got to GitHub first.
    run = await api_client.get_run_if_missing_returns_none(grant.grant_run_id)
    if run is not None and _dispatch_settled(run):
        log.info(
            "temporary_access_grant_deploy_dispatch_settled",
            grant_run_id=grant.grant_run_id,
            claimed_at=withdrawal.claimed_at.isoformat(),
        )
        return True

    await _report_unsettled_dispatch(api_client, grant, withdrawal.claimed_at, log)
    return False


def _dispatch_settled(run: RunDTO) -> bool:
    """Whether the worker that owned this deploy has recorded what it did.

    It writes a typed result on every path it can leave a deploy by, cancelled
    ones included, and only once it is done with it. Until that result is there,
    a dispatch to GitHub Actions may still be ahead of it; after it, whatever
    exists on Actions is listable and a fence reaches it.
    """
    return run.status in TERMINAL_RUN_STATUSES and run.result is not None


async def _report_unsettled_dispatch(
    api_client: SchedulerAPIClient,
    grant: TemporaryAccessGrantDTO,
    claimed_at: datetime,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Say out loud that a claimed grant deploy has gone quiet.

    Ordinarily this is a tick or two while the worker stops its own Actions run.
    A claim that stays unanswered past the stale bound is a worker that is not
    coming back, and the access it may have applied cannot be revoked until
    something outside this sweep deals with it. That is worth an event and an
    admin, not a quiet retry loop.
    """
    waited = _age_minutes(claimed_at)
    log.warning(
        "temporary_access_grant_deploy_dispatch_unsettled",
        grant_run_id=grant.grant_run_id,
        claimed_at=claimed_at.isoformat(),
        waited_minutes=round(waited, 1),
    )
    if waited < _revoke_stale_minutes():
        return
    if (grant.last_error or "").startswith(_UNSETTLED_DISPATCH_ERROR):
        # Already reported for this grant; repeating it every tick would bury it.
        return

    error = (
        f"{_UNSETTLED_DISPATCH_ERROR}: grant deploy {grant.grant_run_id} claimed the dispatch "
        f"{round(waited)} minutes ago and never recorded an outcome"
    )
    log.error(
        "temporary_access_grant_deploy_dispatch_stuck",
        grant_run_id=grant.grant_run_id,
        waited_minutes=round(waited, 1),
    )
    await notify_admins_best_effort(
        f"Temporary access {grant.env_key} for project {grant.project_id} cannot be revoked "
        f"(grant {grant.id}): deploy {grant.grant_run_id} claimed the dispatch "
        f"{round(waited)} minutes ago and never said whether it reached GitHub",
        level="error",
        component="temporary_access",
        grant_id=grant.id,
    )
    await api_client.update_temporary_access_grant(
        grant.id, TemporaryAccessGrantUpdate(last_error=error)
    )


async def _release_qa(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    grant: TemporaryAccessGrantDTO,
    counts: dict[str, int],
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Start the QA run the access was borrowed for.

    The handoff is published after the access is confirmed and stamped after it
    is published, so an interruption in between repeats the publish. The run id
    is fixed on the record, so a repeat lands on the same run rather than
    creating a second one.
    """
    if grant.qa_dispatched_at is not None:
        return
    await redis_client.publish_message(QA_QUEUE, grant.qa_message)
    await api_client.update_temporary_access_grant(
        grant.id, TemporaryAccessGrantUpdate(qa_dispatched=True)
    )
    log.info("temporary_access_qa_released", subject=grant.subject)
    counts["released"] += 1


async def _settle_granted(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    grant: TemporaryAccessGrantDTO,
    counts: dict[str, int],
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Decide a grant that holds confirmed access: release QA, or take it back."""
    if grant.status is TemporaryAccessStatus.REVOKE_FAILED:
        # The reason it must go was decided when the first revoke was dispatched;
        # re-deriving it here would relabel a failed grant as a finished run.
        if grant.revoke_reason is None:
            raise ValueError(f"grant {grant.id} failed to revoke without a reason")
        await _dispatch_revoke(api_client, redis_client, grant, grant.revoke_reason, log)
        counts["dispatched"] += 1
        return

    reason = await _revocation_reason(api_client, grant, log)
    if reason is None:
        await _release_qa(api_client, redis_client, grant, counts, log)
        return
    if reason is TemporaryAccessRevokeReason.EXPIRED:
        counts["expired"] += 1
    await _dispatch_revoke(api_client, redis_client, grant, reason, log)
    counts["dispatched"] += 1


async def _revocation_reason(
    api_client: SchedulerAPIClient,
    grant: TemporaryAccessGrantDTO,
    log: structlog.stdlib.BoundLogger,
) -> TemporaryAccessRevokeReason | None:
    """Why this grant must go now, or None while its run is still live.

    A grant whose run is gone is not waiting for anything, and a grant older than
    the configured lifetime is not waiting for anything either. Whatever holds
    its run open, the access was meant to be temporary.
    """
    run = await api_client.get_run_if_missing_returns_none(grant.qa_run_id)
    if run is None:
        log.warning("temporary_access_run_missing")
        return TemporaryAccessRevokeReason.RUN_MISSING

    if run.status in TERMINAL_RUN_STATUSES:
        log.info("temporary_access_run_terminal", run_status=run.status.value)
        return TemporaryAccessRevokeReason.RUN_TERMINAL

    age = _age_minutes(grant.granted_at)
    if age >= _grant_ttl_minutes():
        # Separate event on purpose: a grant that outlived its run's lifetime
        # means something upstream stopped reporting, and that is worth seeing
        # even though the access itself is handled.
        log.warning(
            "temporary_access_grant_expired",
            age_minutes=round(age, 1),
            ttl_minutes=_grant_ttl_minutes(),
            run_status=run.status.value,
        )
        await notify_admins_best_effort(
            f"Temporary access {grant.env_key} for project {grant.project_id} outlived its "
            f"QA run {grant.qa_run_id} by {round(age)} minutes and is being revoked by timeout",
            level="warning",
            component="temporary_access",
            grant_id=grant.id,
        )
        # The run is still open and the access it was given is going away, so it
        # ends here rather than continuing against a bot that now refuses it.
        await _fail_qa_run(
            api_client,
            grant,
            category=QABlockerCategory.QA_ACCESS_EXPIRED,
            attempted=f"keep temporary access {grant.env_key} for the duration of the QA run",
            sent=f"grant {grant.id} issued {round(age)} minutes ago",
            received=f"QA run still {run.status.value} after {_grant_ttl_minutes()} minutes",
            summary="temporary test access outlived the QA run it was granted for",
            error_message=(
                f"temporary access {grant.env_key} expired while the QA run was still "
                f"{run.status.value}"
            ),
            log=log,
        )
        return TemporaryAccessRevokeReason.EXPIRED

    return None


async def _dispatch_revoke(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    grant: TemporaryAccessGrantDTO,
    reason: TemporaryAccessRevokeReason,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Deploy the granted commit again with the value cleared.

    Same commit, same bot, one value removed, so the identity loses access
    rather than merely disappearing from a config somewhere. The record
    moves to REVOKING before the message is published, so an interrupted publish
    is a stale in-flight revoke the next sweep re-dispatches, not a lost one.
    """
    attempts = grant.revoke_attempts + 1
    revoke_run_id = _new_deploy_run_id("revoke")
    await api_client.update_temporary_access_grant(
        grant.id,
        TemporaryAccessGrantUpdate(
            status=TemporaryAccessStatus.REVOKING,
            revoke_reason=reason,
            revoke_run_id=revoke_run_id,
            revoke_attempts=attempts,
        ),
    )
    await _publish_deploy(
        api_client,
        redis_client,
        grant=grant,
        run_id=revoke_run_id,
        value="",
        run_metadata={"triggered_by": "temporary_access_revoke", "revoke_reason": reason.value},
        # The grant deploy this revoke replaces may still be running on GitHub
        # Actions — that is exactly the case where the grant was abandoned
        # unconfirmed. Clearing the value while it can still be written back
        # would mark the grant revoked with the identity still admitted, so the
        # revoke deploy stops it first and fails if it cannot.
        fence_active_deploys=True,
    )
    log.info(
        "temporary_access_revoke_dispatched",
        reason=reason.value,
        revoke_run_id=revoke_run_id,
        attempt=attempts,
        head_sha=grant.head_sha,
    )


async def _settle_revoke_in_flight(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    grant: TemporaryAccessGrantDTO,
    counts: dict[str, int],
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Read the dispatched revoke deploy and close, retry, or fail the grant."""
    if grant.revoke_run_id is None:
        raise ValueError(f"grant {grant.id} is revoking without a revoke run")
    if grant.revoke_reason is None:
        raise ValueError(f"grant {grant.id} is revoking without a reason")

    run = await api_client.get_run_if_missing_returns_none(grant.revoke_run_id)
    if run is None:
        log.warning("temporary_access_revoke_run_missing", revoke_run_id=grant.revoke_run_id)
        await _dispatch_revoke(api_client, redis_client, grant, grant.revoke_reason, log)
        counts["dispatched"] += 1
        return

    if run.status not in TERMINAL_RUN_STATUSES:
        age = _age_minutes(run.created_at)
        if age < _revoke_stale_minutes():
            return
        # The deploy neither finished nor failed. Whatever was carrying it is not
        # coming back, so the access is still out there: dispatch a new one.
        log.warning(
            "temporary_access_revoke_stale",
            revoke_run_id=run.id,
            run_status=run.status.value,
            age_minutes=round(age, 1),
        )
        await _dispatch_revoke(api_client, redis_client, grant, grant.revoke_reason, log)
        counts["dispatched"] += 1
        return

    if run.status is RunStatus.CANCELLED:
        # A revoke that lost the project's deploy lock was superseded, not
        # refused. The access is still out, so dispatch again; calling that a
        # failure would pin a contention window on the QA run.
        log.info("temporary_access_revoke_superseded", revoke_run_id=run.id)
        await _dispatch_revoke(api_client, redis_client, grant, grant.revoke_reason, log)
        counts["dispatched"] += 1
        return

    if _deploy_succeeded(run):
        await api_client.update_temporary_access_grant(
            grant.id, TemporaryAccessGrantUpdate(status=TemporaryAccessStatus.REVOKED)
        )
        log.info(
            "temporary_access_revoked",
            revoke_run_id=run.id,
            reason=grant.revoke_reason.value,
            attempts=grant.revoke_attempts,
        )
        counts["revoked"] += 1
        return

    await _record_revoke_failure(api_client, grant, run, counts, log)


async def _record_revoke_failure(
    api_client: SchedulerAPIClient,
    grant: TemporaryAccessGrantDTO,
    run: RunDTO,
    counts: dict[str, int],
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Keep a failed revoke retryable, and stop hiding it once retries run long.

    A single failed deploy is retried quietly: the access is still marked as held
    and the next sweep dispatches again. Once the configured attempts are spent,
    the failure stops being an internal retry and becomes the QA run's, which is
    what lets the story reach a visible outcome instead of waiting on a revoke
    that keeps failing.

    The QA run is failed before the grant is stamped as escalated, and never the
    other way round. The stamp is what stops the story from waiting on this
    grant, so writing it first would open that gate with the QA run still saying
    the run passed — one dead process in between and a story publishes success
    while the identity is still admitted. In the order below the crash window
    leaves the story waiting, which the next sweep resolves.
    """
    error = _deploy_failure_detail(run, "revoke")
    exhausted = grant.revoke_attempts >= _max_revoke_attempts() and grant.escalated_at is None
    log.error(
        "temporary_access_revoke_failed",
        revoke_run_id=run.id,
        attempts=grant.revoke_attempts,
        error=error,
        escalated=exhausted,
    )
    counts["revoke_failed"] += 1
    if not exhausted:
        await api_client.update_temporary_access_grant(
            grant.id,
            TemporaryAccessGrantUpdate(
                status=TemporaryAccessStatus.REVOKE_FAILED, last_error=error
            ),
        )
        return

    await notify_admins_best_effort(
        f"Temporary access {grant.env_key} for project {grant.project_id} survived "
        f"{grant.revoke_attempts} revoke attempts (grant {grant.id}); the test identity "
        "may still have access",
        level="error",
        component="temporary_access",
        grant_id=grant.id,
    )
    await _fail_qa_run(
        api_client,
        grant,
        category=QABlockerCategory.QA_CLEANUP_FAILED,
        attempted=f"revoke temporary access {grant.env_key} for project {grant.project_id}",
        sent=f"deploy of {grant.head_sha} with {grant.env_key} cleared",
        received=error,
        summary="temporary test access could not be revoked",
        error_message=f"temporary access {grant.env_key} is still granted: {error}",
        log=log,
    )
    await api_client.update_temporary_access_grant(
        grant.id,
        TemporaryAccessGrantUpdate(
            status=TemporaryAccessStatus.REVOKE_FAILED, last_error=error, escalated=True
        ),
    )


def _deploy_succeeded(run: RunDTO) -> bool:
    """Only a completed deploy that reported SUCCESS actually moved the value."""
    if run.status is not RunStatus.COMPLETED or run.result is None:
        return False
    return run.result.deploy_outcome is DeployOutcome.SUCCESS


def _deploy_failure_detail(run: RunDTO, kind: str) -> str:
    outcome = run.result.deploy_outcome.value if run.result is not None else "no result"
    return f"{kind} deploy {run.id} ended {run.status.value} ({outcome})"


async def _fail_qa_run(
    api_client: SchedulerAPIClient,
    grant: TemporaryAccessGrantDTO,
    *,
    category: QABlockerCategory,
    attempted: str,
    sent: str,
    received: str,
    summary: str,
    error_message: str,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Access that could not be handed over or taken back is the run's failure.

    It is not a reason to stop the scheduler and not something to leave as a log
    line next to a successful run: the QA run that borrowed the identity carries
    the failure, with the grant named, and the grant stays live for the sweep to
    keep working on.
    """
    if grant.revoke_reason is TemporaryAccessRevokeReason.RUN_MISSING:
        # There is no run left to carry the failure; the grant itself is the
        # only record, and it already holds the error.
        log.warning("temporary_access_failure_has_no_run", grant_id=grant.id)
        return

    await api_client.update_run(
        grant.qa_run_id,
        {
            "status": RunStatus.FAILED.value,
            "error_message": error_message,
            "result": QARunResult(
                qa_outcome=QAOutcome.BLOCKED,
                summary=summary,
                blocker=QABlocker(
                    category=category, attempted=attempted, sent=sent, received=received
                ),
            ).model_dump(mode="json"),
        },
    )
    log.warning("temporary_access_qa_run_failed", grant_id=grant.id, blocker=category.value)

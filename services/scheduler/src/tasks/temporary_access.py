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

What "it landed" means for the revoke is the one thing that cannot be decided
from inside this system. Between the decision and the effect stands GitHub
Actions: it is not ours, it works asynchronously, and no amount of stopping
writers here proves that none of them will apply the old value afterwards. So
the grant is not closed on a successful revoke deploy. It is closed on readings
of the environment the service is actually running with, taken from the server
itself — and on more than one of them, taken apart over a window, because a
single empty reading is a moment and a late dispatch lands after moments. Until
those readings agree, the grant stays live in REVOKING and the sweep keeps
revoking; a value that comes back is then simply a disagreement between what we
want and what we see, and the cycle that sees it corrects it.

Closing the grant does not stop the readings. The same writer that could land
between two readings can land after the last one, so the slot is read for the
whole watch window after the record closed, and a value found there reopens the
grant and is revoked again. What the system promises is therefore not "the
access can never come back" but "the access does not outlive one reconciliation
interval after it is seen".

None of this holds a finished product back. The story the access was borrowed
for is routed on what QA said about the product, and it completes on the tick
that reads that verdict whether or not the identity has been handed back. The
sweep keeps working afterwards on its own schedule, and when it runs out of
attempts it says so to an administrator instead of to the user — a test identity
left behind is a cleanup incident, and the counts below are what tells one still
being chased from one that has been given up on.

That promise is made at two speeds, because the writer it is meant to handle is
not bounded by our windows. The cooling-off window above is the fast one, paced
in minutes and worth an ssh that often. Under it runs the slow one: the slot the
contract declares must be empty while no grant holds it, and that is checked on
its own cadence, counted in hours, for as long as the slot exists. A value that
lands a minute after the fast watch expires is not lost — it waits for the slow
check, is revoked by the same code, and fails the same way visibly if it stays.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, NamedTuple
import uuid

import structlog

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.deploy_dispatch import DISPATCH_SUPERSEDED_AT_KEY
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
    TemporaryAccessObservation,
    TemporaryAccessRevokeReason,
    TemporaryAccessStatus,
)
from shared.contracts.queues.deploy import (
    DeployAction,
    DeployMessage,
    DeployOutcome,
    DeployTrigger,
)
from shared.contracts.queues.env_observation import (
    EnvObservationOutcome,
    EnvObservationRequest,
    EnvObservationResult,
    env_observation_pending_key,
    env_observation_result_key,
)
from shared.contracts.queues.qa import QAMessage, QAOutcome
from shared.notifications import notify_admins_best_effort
from shared.queues import DEPLOY_QUEUE, ENV_OBSERVATION_QUEUE, QA_QUEUE
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

# Same idea for a grant whose revoke landed but whose server cannot be read.
_UNOBSERVABLE_ERROR = "temporary access cannot be observed"


def _empty_counts() -> dict[str, int]:
    """What this tick did, split so a stuck cleanup can be seen for what it is.

    ``revoke_failed`` is an attempt that did not land and will be tried again —
    the cleanup is still working. ``escalated`` is the sweep giving up on a grant
    and calling a human. They are counted apart on purpose: the story the access
    was borrowed for no longer waits for either, so these counts are what says
    whether a test identity is still being chased or has been left to somebody.
    """
    return {
        "dispatched": 0,
        "released": 0,
        "revoked": 0,
        "expired": 0,
        "revoke_failed": 0,
        "escalated": 0,
    }


def _grant_ttl_minutes() -> int:
    return startup.get_config().get_int("supervisor.temporary_access_ttl_minutes")


def _revoke_stale_minutes() -> int:
    return startup.get_config().get_int("supervisor.temporary_access_revoke_stale_minutes")


def _max_revoke_attempts() -> int:
    return startup.get_config().get_int("supervisor.temporary_access_max_revoke_attempts")


def _observation_window_minutes() -> int:
    return startup.get_config().get_int("supervisor.temporary_access_observation_window_minutes")


def _unrevoked_ttl_minutes() -> int:
    return startup.get_config().get_int("supervisor.temporary_access_unrevoked_ttl_minutes")


def _revoked_watch_minutes() -> int:
    return startup.get_config().get_int("supervisor.temporary_access_revoked_watch_minutes")


def _contract_audit_hours() -> int:
    return startup.get_config().get_int("supervisor.temporary_access_contract_audit_hours")


def _slot_audit_key(grant: TemporaryAccessGrantDTO) -> str:
    """Marks a slot whose slow check has been attempted in this interval.

    The due set is decided from when the slot was last read, and a slot that
    cannot be read is never read — so without this a project whose server is down
    or whose application is gone would be worked on every tick forever. The
    marker expires with the interval, which is what makes the slow check cost one
    attempt per slot per interval whether or not anything answers.
    """
    return f"temporary-access:slot-audit:{grant.project_id}:{grant.env_key}"


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

    Which deploy carries the access is decided by that record and never by this
    call. The proposed id is only a proposal: a retry gets the stored one back,
    and everything after this point uses it. Deploying under the freshly made id
    instead would put the identity on the application through a run the record
    does not name — the sweep would watch the first deploy, revoke against it,
    and the second one would write the value back afterwards with nothing left
    watching.

    For the same reason the deploy is published only while the record still says
    the access is being handed out and no run yet carries it. A repeat that finds
    either otherwise is a handoff the sweep has already taken over, and the sweep
    is what redispatches, expires and revokes it from there.
    """
    grant_id = f"tempaccess-{qa_message.run_id}"[:255]
    grant = await api_client.create_temporary_access_grant(
        TemporaryAccessGrantCreate(
            id=grant_id,
            project_id=project_id,
            env_key=env_key,
            subject=subject,
            head_sha=head_sha,
            qa_run_id=qa_message.run_id,
            grant_run_id=_new_deploy_run_id("grant"),
            qa_message=qa_message,
        )
    )

    log = logger.bind(
        grant_id=grant.id,
        project_id=project_id,
        env_key=env_key,
        qa_run_id=grant.qa_run_id,
    )
    if grant.status is not TemporaryAccessStatus.GRANTING:
        log.info(
            "temporary_access_grant_already_owned_by_sweep",
            grant_status=grant.status.value,
            grant_run_id=grant.grant_run_id,
        )
        return grant
    if await api_client.get_run_if_missing_returns_none(grant.grant_run_id) is not None:
        log.info(
            "temporary_access_grant_deploy_already_dispatched", grant_run_id=grant.grant_run_id
        )
        return grant

    await _publish_grant_deploy(api_client, redis_client, grant, grant.grant_run_id)
    log.info(
        "temporary_access_granting",
        head_sha=grant.head_sha,
        grant_run_id=grant.grant_run_id,
    )
    return grant


async def supervise_temporary_access(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """Settle every grant that still holds access, and read the closed slots.

    A closed grant is read for a while longer. The record closed it on readings,
    and readings are moments: a dispatch that was already on its way to GitHub
    Actions can put the value back afterwards. So the slot keeps being read for
    the watch window, and a value found there puts the grant back under
    reconciliation instead of standing on a bot nobody is looking at.

    Past that window the slot is still read, only rarely: the contract says the
    key is empty while no grant holds it, and that stays true whether or not any
    grant is recent. The slow reading costs an ssh and a playbook per slot, so it
    is asked for once per ``supervisor.temporary_access_contract_audit_hours``
    rather than once per tick. A value found by it goes down exactly the same
    path as one found by the fast watch.

    Returns counts of the actions taken this tick.
    """
    now = datetime.now(UTC)
    watch_from = now - timedelta(minutes=_revoked_watch_minutes())
    audit_before = now - timedelta(hours=_contract_audit_hours())
    grants = await api_client.list_temporary_access_grants_under_watch(watch_from, audit_before)
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
            elif grant.status is TemporaryAccessStatus.REVOKED:
                await _watch_closed_grant(api_client, redis_client, grant, counts, log)
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
            # A temporary-access deploy is machinery, not something the owner
            # asked for: it carries no recipient on purpose.
            unaddressed_reason="temporary QA access deploy, not requested by the owner",
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

    A deploy that did cross is normally waited for: the worker writes a typed
    result on every path it can leave a deploy by, and that result is proof that
    whatever it put on GitHub Actions is listable, so a fence reaches it.

    Waiting on a worker that is never coming back is the case this must survive,
    and it ends on the claim's lease: the holder promised not to dispatch past
    its deadline, so once the lease has run out the claim is taken back rather
    than waited on, and a claim taken back can neither dispatch nor be renewed.

    That promise is a worker's own clock, and none of this is proof. A paused
    worker still reaches ``workflow_dispatch`` afterwards, and nothing here can
    rule it out. Withdrawing, superseding and fencing narrow the window in which
    the old value can be written back; what says it is gone is the reading of the
    running service, and until that reading is made the grant stays live and the
    sweep keeps revoking.

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

    # Claimed. The withdrawal cancelled the run, so a worker that is still alive
    # stops and records what it did. Only it knows whether it got to GitHub, so
    # the next read is the cheap way out.
    run = await api_client.get_run_if_missing_returns_none(grant.grant_run_id)
    if run is not None and _dispatch_settled(run):
        log.info(
            "temporary_access_grant_deploy_dispatch_settled",
            grant_run_id=grant.grant_run_id,
            claimed_at=withdrawal.claimed_at.isoformat(),
        )
        return True

    supersede = await api_client.supersede_deploy_dispatch(
        grant.grant_run_id,
        f"temporary access grant {grant.id} took the dispatch back: {detail}",
    )
    if supersede.settled:
        log.warning(
            "temporary_access_grant_deploy_dispatch_superseded",
            grant_run_id=grant.grant_run_id,
            outcome=supersede.outcome.value,
            claimed_at=withdrawal.claimed_at.isoformat(),
        )
        return True

    await _report_unsettled_dispatch(api_client, grant, withdrawal.claimed_at, log)
    return False


def _dispatch_settled(run: RunDTO) -> bool:
    """Whether this deploy can still put something on GitHub Actions unseen.

    The worker writes a typed result on every path it can leave a deploy by,
    cancelled ones included, and only once it is done with it. That result is
    the ordinary proof.

    A claim taken back is the other one. It says the lease ran out and the
    boundary was closed against the holder, so the holder cannot re-claim,
    whether or not it is alive to be asked. Both mean the same thing to the
    revoke: it may go out now instead of waiting for a process that may be gone.
    Neither means nothing can write the old value afterwards — a taken-back
    holder can still be delivering a dispatch nobody here can see. That is what
    the reading of the running service is for; this only decides when to stop
    waiting.
    """
    if run.run_metadata.get(DISPATCH_SUPERSEDED_AT_KEY):
        return True
    return run.status in TERMINAL_RUN_STATUSES and run.result is not None


async def _report_unsettled_dispatch(
    api_client: SchedulerAPIClient,
    grant: TemporaryAccessGrantDTO,
    claimed_at: datetime,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Say out loud that a claimed grant deploy is still holding the boundary.

    Ordinarily this is a tick or two while the worker stops its own Actions run,
    and a worker that never comes back loses the claim when its lease runs out.
    So getting here past the stale bound means the lease is being renewed by
    something that is not finishing — which the sweep cannot resolve on its own,
    and which is worth an event and an admin rather than a quiet retry loop.
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
        # unconfirmed. Stopping it shortens the time in which it can write the
        # identity back; it does not prove nothing will. The grant is closed by
        # the reading of the running service, not by this.
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

    if not _deploy_succeeded(run):
        await _record_revoke_failure(
            api_client, grant, _deploy_failure_detail(run, "revoke"), counts, log
        )
        return

    # The deploy is only the request. What settles the grant is the environment
    # the service is running with, read from the server — and read more than
    # once, because one empty reading is a moment a late writer can still land
    # after. The record holds the streak and decides; this reports the reading.
    read = await _observe_running_service(api_client, redis_client, grant, log)
    if read is None:
        # Nothing was read. Not a revocation and not a failure: the grant stays
        # live in REVOKING and the next tick asks again.
        return
    observation, settled = read

    if observation.present:
        await _record_revoke_failure(
            api_client,
            grant,
            f"the running service still carries {grant.env_key} after revoke deploy {run.id}",
            counts,
            log,
        )
        return

    if settled.status is not TemporaryAccessStatus.REVOKED:
        # Read empty, and still under reconciliation. The next readings decide;
        # until they agree over the confirmation window the grant stays live and
        # a value that comes back is seen.
        log.info(
            "temporary_access_revoke_awaiting_confirmation",
            revoke_run_id=run.id,
            readings=settled.slot_clear_readings,
            clear_since=settled.slot_clear_since.isoformat() if settled.slot_clear_since else None,
        )
        return

    log.info(
        "temporary_access_revoked",
        revoke_run_id=run.id,
        reason=grant.revoke_reason.value,
        attempts=grant.revoke_attempts,
        readings=settled.slot_clear_readings,
    )
    counts["revoked"] += 1


async def _watch_closed_grant(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    grant: TemporaryAccessGrantDTO,
    counts: dict[str, int],
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Keep reading the slot of a grant the readings already closed.

    Closing is what the readings said at the time, and a writer we do not own can
    land after them: a ``workflow_dispatch`` accepted by GitHub Actions before
    the revoke, a hand-run playbook, a redeploy of the wrong commit. None of that
    is visible from here, and all of it is visible in the environment the service
    is running with.

    So a reading that finds the value puts the grant back under reconciliation —
    the record does that, not this — and the revoke goes out again on the same
    tick. The story the grant belonged to has usually finished by then; that is
    the point. What was promised is not that the value can never come back, but
    that it does not stay: the sweep that sees it takes it off.

    Two cadences reach here and the handling is deliberately one. Inside the
    cooling-off window the slot is read every observation window, because that is
    where a dispatch already in flight lands. Outside it the same slot is read
    once per audit interval, because the invariant it is checked against — the
    key is empty while no grant holds it — does not expire with the window. A
    value that came back an hour after the record closed and one that came back a
    week after are the same value that should not be there.
    """
    read = await _observe_running_service(
        api_client, redis_client, grant, log, slow_audit=_past_the_watch_window(grant)
    )
    if read is None:
        return
    observation, settled = read

    if not observation.present:
        return

    if settled.status is not TemporaryAccessStatus.REVOKING:
        # The record kept the grant closed, which means the slot has a live owner
        # again: a later grant holds this key on purpose and is reconciled on its
        # own. Revoking from here would take that grant's value off under it.
        log.info(
            "temporary_access_observed_value_belongs_to_another_grant",
            grant_status=settled.status.value,
        )
        return

    log.error(
        "temporary_access_value_returned_after_revoke",
        revoked_at=grant.revoked_at.isoformat() if grant.revoked_at else None,
        observation_id=settled.observation_id,
        slow_audit=_past_the_watch_window(grant),
    )
    await notify_admins_best_effort(
        f"Temporary access {grant.env_key} for project {grant.project_id} (grant {grant.id}) "
        "is set on the running service again after it was confirmed removed; revoking it again",
        level="error",
        component="temporary_access",
        grant_id=grant.id,
    )
    await _dispatch_revoke(
        api_client,
        redis_client,
        settled,
        TemporaryAccessRevokeReason.OBSERVED_AFTER_REVOKE,
        log,
    )
    counts["dispatched"] += 1


def _past_the_watch_window(grant: TemporaryAccessGrantDTO) -> bool:
    """Whether this closed grant is here for the slow check rather than the watch.

    A grant closed within the cooling-off window is being watched, and the watch
    is paced in minutes. Past it, the record is only here because the slot it
    owns is due a reading, and that is paced in hours.
    """
    if grant.revoked_at is None:
        return False
    return _age_minutes(grant.revoked_at) >= _revoked_watch_minutes()


def _observation_request_id(grant: TemporaryAccessGrantDTO) -> str:
    """One question per reading, named after the revoke attempt and the streak.

    A new revoke gets a new run id, so it also gets new questions; an answer to
    the previous attempt can never be mistaken for an answer to this one. The
    streak length distinguishes the readings within one attempt, so confirming a
    revoke is several separate questions rather than one answer re-read.
    """
    return f"envobs-{grant.revoke_run_id}-{grant.slot_clear_readings}"


async def _observe_running_service(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    grant: TemporaryAccessGrantDTO,
    log: structlog.stdlib.BoundLogger,
    *,
    slow_audit: bool = False,
) -> tuple[EnvObservationResult, TemporaryAccessGrantDTO] | None:
    """Take one reading of the running service and record it against the grant.

    Returns the reading and the grant as the record left it, or None when there
    is no reading — the request has only just gone out, the window since the last
    one has not passed, or the answer is that the service could not be reached.

    None is deliberately not a third kind of failure. A channel that is down says
    nothing about the access, so the caller neither closes the grant nor counts
    an attempt against it; it asks again next tick.
    """
    request_id = _observation_request_id(grant)
    observation = await _read_observation(redis_client, request_id)
    if observation is None:
        await _ask_for_observation(
            api_client, redis_client, grant, request_id, log, slow_audit=slow_audit
        )
        return None

    # The answer has been taken, so the next reading is a fresh question rather
    # than this one read twice. What paces the next question is the moment this
    # reading is stamped on the record.
    await redis_client.redis.delete(env_observation_result_key(request_id))

    if observation.outcome is EnvObservationOutcome.UNREACHABLE:
        log.warning(
            "temporary_access_observation_unreachable",
            request_id=request_id,
            detail=observation.detail,
        )
        await _report_unobservable_grant(api_client, grant, observation.detail, log)
        return None

    target = await _observation_target(api_client, grant, log)
    if target is None:
        # The application went away between asking and answering. The reading
        # cannot be attributed to a deployment, so it is not evidence.
        return None

    log.info(
        "temporary_access_observed",
        request_id=request_id,
        present=observation.present,
        containers=observation.containers,
    )
    settled = await api_client.record_temporary_access_observation(
        grant.id,
        TemporaryAccessObservation(
            observation_id=request_id,
            application_id=target.application_id,
            server_handle=target.server_handle,
            service_slug=target.service_slug,
            env_key=grant.env_key,
            present=observation.present,
            containers=observation.containers,
        ),
    )
    return observation, settled


async def _read_observation(
    redis_client: RedisStreamClient, request_id: str
) -> EnvObservationResult | None:
    stored = await redis_client.redis.get(env_observation_result_key(request_id))
    if stored is None:
        return None
    return EnvObservationResult.model_validate_json(stored)


async def _ask_for_observation(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    grant: TemporaryAccessGrantDTO,
    request_id: str,
    log: structlog.stdlib.BoundLogger,
    *,
    slow_audit: bool = False,
) -> None:
    """Ask the server what the service is running with, once per window.

    Two things keep one question from becoming a queue of them. The marker stops
    a question being asked twice while the playbook is still running; the moment
    of the last recorded reading stops the next question following the previous
    answer immediately. Losing either costs a repeated reading, and a reading
    changes nothing.

    A slow check is paced by neither. What makes a slot due is its last reading,
    and a slot that cannot be read produces no reading — a server that is down, an
    application that has been taken away — so it would be worked on every tick
    until that changed. Its own marker is what holds the interval, and it is taken
    before anything is looked up rather than before the question goes out: an
    unreadable slot costs the same one attempt per interval as a readable one,
    instead of a round of lookups per tick for every project that ever held a
    grant.
    """
    if grant.observed_at is not None and _age_minutes(grant.observed_at) < (
        _observation_window_minutes()
    ):
        return

    if slow_audit and not await redis_client.redis.set(
        _slot_audit_key(grant),
        request_id,
        nx=True,
        ex=_contract_audit_hours() * 3600,
    ):
        return

    target = await _observation_target(api_client, grant, log)
    if target is None:
        return

    asked = await redis_client.redis.set(
        env_observation_pending_key(request_id),
        request_id,
        nx=True,
        ex=_observation_window_minutes() * 60,
    )
    if not asked:
        return

    await redis_client.publish_message(
        ENV_OBSERVATION_QUEUE,
        EnvObservationRequest(
            request_id=request_id,
            project_id=grant.project_id,
            server_handle=target.server_handle,
            service_slug=target.service_slug,
            env_key=grant.env_key,
        ),
    )
    log.info(
        "temporary_access_observation_requested",
        request_id=request_id,
        server_handle=target.server_handle,
        application_id=target.application_id,
        slow_audit=slow_audit,
    )


class _ObservationTarget(NamedTuple):
    """The one deployment whose environment answers this grant's question."""

    application_id: int
    server_handle: str
    service_slug: str


async def _observation_target(
    api_client: SchedulerAPIClient,
    grant: TemporaryAccessGrantDTO,
    log: structlog.stdlib.BoundLogger,
) -> _ObservationTarget | None:
    """Which deployment to read, or None if there is nothing readable.

    The application is the one the QA run tested, named by the handoff the grant
    carries. Applications are unique per (repository, server), so a project can
    be running on several machines at once; reading whichever one comes back
    first would let an empty slot on an unrelated deployment close a grant whose
    bot still admits the test identity.

    An application that is not running is not an environment that can be read,
    and neither is one that is gone. Both are the observation channel being
    unavailable rather than access that is gone.
    """
    application_id = grant.qa_message.application_id
    application = await api_client.get_application_if_missing_returns_none(application_id)
    if application is None:
        log.warning("temporary_access_observation_no_application", application_id=application_id)
        return None

    repositories = await api_client.get_repositories(grant.project_id)
    if application.repo_id not in {repository.id for repository in repositories}:
        # The handoff names a deployment of another project. Nothing about this
        # grant can be read from it, and quietly reading it anyway is how a
        # grant gets closed on the wrong machine.
        raise ValueError(
            f"grant {grant.id} names application {application_id}, "
            f"which belongs to repository {application.repo_id} outside project "
            f"{grant.project_id}"
        )

    if application.status is not ApplicationStatus.RUNNING:
        log.warning(
            "temporary_access_observation_application_not_running",
            application_id=application_id,
            application_status=application.status.value,
        )
        return None

    project = await api_client.get_project(grant.project_id)
    if project is None:
        log.warning("temporary_access_observation_no_project")
        return None
    return _ObservationTarget(application.id, application.server_handle, project.slug)


async def _report_unobservable_grant(
    api_client: SchedulerAPIClient,
    grant: TemporaryAccessGrantDTO,
    detail: str,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Say out loud that a grant is being kept live by an unreadable server.

    It is not a failure of the revoke and it does not settle anything, so it
    stays out of the grant's outcome. But a grant that quietly waits forever for
    a channel that is never coming back is worse than one that is complained
    about, so past the same window that bounds everything else here it is said
    once.
    """
    if grant.status is TemporaryAccessStatus.REVOKED:
        # The readings that closed this grant said the access is gone. Reading it
        # again is the watch for a value that comes back, and a server that
        # cannot be reached during it is not a grant being kept live by one —
        # there is nothing left to complain about, and the record refuses to be
        # written anyway.
        return
    if _age_minutes(_reconciling_since(grant)) < _unrevoked_ttl_minutes():
        return
    if (grant.last_error or "").startswith(_UNOBSERVABLE_ERROR):
        return

    error = f"{_UNOBSERVABLE_ERROR}: {detail}"
    log.error("temporary_access_observation_channel_down", detail=detail)
    await notify_admins_best_effort(
        f"Temporary access {grant.env_key} for project {grant.project_id} (grant {grant.id}) "
        f"cannot be confirmed removed: {detail}",
        level="error",
        component="temporary_access",
        grant_id=grant.id,
    )
    await api_client.update_temporary_access_grant(
        grant.id, TemporaryAccessGrantUpdate(last_error=error)
    )


def _retries_are_spent(grant: TemporaryAccessGrantDTO) -> bool:
    """Whether this grant has stopped being an internal retry.

    Either bound ends it: the attempts budget, or the grant living past the age
    at which an unrevoked one stops being a hiccup. The second one matters
    because a disagreement between what we deployed and what we observe can
    repeat cheaply and forever, and a story must not wait on it forever.
    """
    return (
        grant.revoke_attempts >= _max_revoke_attempts()
        or _age_minutes(_reconciling_since(grant)) >= _unrevoked_ttl_minutes()
    )


def _reconciling_since(grant: TemporaryAccessGrantDTO) -> datetime:
    """When the disagreement being worked on now started.

    Usually the grant itself. A grant that was closed and then found holding the
    value again is a new disagreement: it starts at the reopening, so the age
    bounds are measured against it and a returned value gets its own attempts
    rather than inheriting an exhausted budget from hours ago.
    """
    return grant.reopened_at or grant.granted_at


async def _record_revoke_failure(
    api_client: SchedulerAPIClient,
    grant: TemporaryAccessGrantDTO,
    error: str,
    counts: dict[str, int],
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Keep an unfinished revoke retryable, and call a human once retries run out.

    Two things arrive here: a revoke deploy that failed, and a revoke deploy that
    succeeded while the server still shows the value. They are the same problem —
    the access is still out — and they are handled the same way. A single one is
    retried quietly: the access is still marked as held and the next sweep
    dispatches again. Once the attempts are spent, or the grant has outlived the
    age at which an unrevoked one stops being a hiccup, retrying quietly stops
    being honest and the incident is handed to an administrator, named by the
    story, the project, the QA run and the grant it belongs to. That message is
    the point of the escalation: a test identity nobody took back is somebody's
    work, and this is where it stops being the sweep's alone.

    It is a cleanup incident and not a product verdict. The story the access was
    borrowed for is routed on what QA said about the product and has usually
    finished long before this; nothing here reopens it. The escalation records
    the incident on the grant and stamps it so the report is made once rather
    than every tick. If QA is still live, the API records the named cleanup
    failure on that run; a settled QA verdict remains authoritative.
    """
    exhausted = _retries_are_spent(grant) and grant.escalated_at is None
    log.error(
        "temporary_access_revoke_failed",
        revoke_run_id=grant.revoke_run_id,
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

    counts["escalated"] += 1
    await notify_admins_best_effort(
        f"Temporary access {grant.env_key} survived {grant.revoke_attempts} revoke attempts "
        f"and needs a human: grant {grant.id}, project {grant.project_id}, "
        f"story {grant.qa_message.story_id}, QA run {grant.qa_run_id}. "
        f"The test identity {grant.subject} may still be admitted by the deployed bot. "
        f"Last error: {error}",
        level="error",
        component="temporary_access",
        grant_id=grant.id,
        story_id=grant.qa_message.story_id,
        qa_run_id=grant.qa_run_id,
    )
    await api_client.escalate_temporary_access_grant(
        grant.id,
        error=error,
        run_error_message=f"temporary access {grant.env_key} is still granted: {error}",
        run_result=QARunResult(
            qa_outcome=QAOutcome.BLOCKED,
            summary="temporary test access could not be revoked",
            blocker=QABlocker(
                category=QABlockerCategory.QA_CLEANUP_FAILED,
                attempted=(
                    f"revoke temporary access {grant.env_key} for project {grant.project_id}"
                ),
                sent=f"deploy of {grant.head_sha} with {grant.env_key} cleared",
                received=error,
            ),
        ),
    )
    log.warning(
        "temporary_access_qa_run_failed",
        grant_id=grant.id,
        blocker=QABlockerCategory.QA_CLEANUP_FAILED.value,
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

    A run that reached its own outcome first keeps it. The sweep and the QA
    worker can both be deciding the same run is over — an expiring grant against
    a run finishing at the same moment — and the first answer is the one that
    happened. Refused here means the run is settled, not that the sweep failed:
    the revoke this precedes goes ahead regardless, because the access is out
    whatever the run ended up saying.
    """
    if grant.revoke_reason is TemporaryAccessRevokeReason.RUN_MISSING:
        # There is no run left to carry the failure; the grant itself is the
        # only record, and it already holds the error.
        log.warning("temporary_access_failure_has_no_run", grant_id=grant.id)
        return

    recorded = await api_client.record_run_outcome_unless_settled(
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
    if not recorded:
        log.warning(
            "temporary_access_qa_run_already_settled",
            grant_id=grant.id,
            blocker=category.value,
        )
        return
    log.warning("temporary_access_qa_run_failed", grant_id=grant.id, blocker=category.value)

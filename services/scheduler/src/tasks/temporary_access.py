"""Temporary access reconciliation: revoke by state, not by happy path.

Access handed to a test identity is revoked by a redeploy of the same commit with
the value cleared. Running that redeploy at the end of a successful QA run makes
revocation likely, not certain: a killed run, a cancelled one, or a dead process
between grant and revoke leaves the access standing with nobody left to remove it.

So the grant is a durable record and this sweep is the only thing that revokes.
It reads every grant that still holds access, decides from the state of the QA run
it was made for, and dispatches the revoke deploy. It repeats until the deploy has
landed, which makes it safe for the sweep itself to be interrupted at any point.
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
from shared.contracts.queues.qa import QAOutcome
from shared.notifications import notify_admins_best_effort
from shared.queues import DEPLOY_QUEUE
from shared.redis_client import RedisStreamClient

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient

from .. import startup

logger = structlog.get_logger(__name__)

# A run in any of these states will never come back to release its grant.
TERMINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})


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
    qa_run_id: str,
) -> tuple[TemporaryAccessGrantDTO, str]:
    """Hand the access out, record first.

    The record is written before the deploy that applies the value, so no order
    of crashes can produce access nobody knows about: the worst case is a record
    for access that was never applied, and revoking that is a redeploy of a value
    that is already empty.

    Returns the stored grant and the id of the deploy run that applies it.
    """
    grant_id = f"tempaccess-{uuid.uuid4().hex[:12]}"
    grant = await api_client.create_temporary_access_grant(
        TemporaryAccessGrantCreate(
            id=grant_id,
            project_id=project_id,
            env_key=env_key,
            subject=subject,
            head_sha=head_sha,
            qa_run_id=qa_run_id,
        )
    )

    deploy_run_id = f"deploy-grant-{uuid.uuid4().hex[:8]}"
    await api_client.create_run(
        {
            "id": deploy_run_id,
            "type": RunType.DEPLOY.value,
            "project_id": project_id,
            "status": RunStatus.QUEUED.value,
            "run_metadata": {
                "triggered_by": "temporary_access_grant",
                "head_sha": head_sha,
                "grant_id": grant.id,
            },
        }
    )
    await redis_client.publish_message(
        DEPLOY_QUEUE,
        DeployMessage(
            task_id=deploy_run_id,
            project_id=project_id,
            user_id="",
            story_id="",
            triggered_by=DeployTrigger.ADMIN,
            action=DeployAction.FEATURE,
            head_sha=head_sha,
            env_overrides={env_key: subject},
        ),
    )
    logger.info(
        "temporary_access_granted",
        grant_id=grant.id,
        project_id=project_id,
        env_key=env_key,
        qa_run_id=qa_run_id,
        head_sha=head_sha,
        deploy_run_id=deploy_run_id,
    )
    return grant, deploy_run_id


async def supervise_temporary_access(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """Settle every grant that still holds access.

    Returns counts of the actions taken this tick.
    """
    grants = await api_client.list_live_temporary_access_grants()
    counts = {"dispatched": 0, "revoked": 0, "expired": 0, "revoke_failed": 0}
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
            if grant.status is TemporaryAccessStatus.REVOKING:
                await _settle_revoke_in_flight(api_client, redis_client, grant, counts, log)
                continue

            reason = await _revocation_reason(api_client, grant, log)
            if reason is None:
                continue
            if reason is TemporaryAccessRevokeReason.EXPIRED:
                counts["expired"] += 1
            await _dispatch_revoke(api_client, redis_client, grant, reason, log)
            counts["dispatched"] += 1
        except Exception:
            # One grant that cannot be settled is that grant's problem. It stays
            # live and is retried next tick; the other grants still get swept.
            log.exception("temporary_access_grant_sweep_error", status=grant.status.value)
            counts["revoke_failed"] += 1

    return counts


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
    revoke_run_id = f"deploy-revoke-{uuid.uuid4().hex[:8]}"
    await api_client.create_run(
        {
            "id": revoke_run_id,
            "type": RunType.DEPLOY.value,
            "project_id": grant.project_id,
            "status": RunStatus.QUEUED.value,
            "run_metadata": {
                "triggered_by": "temporary_access_revoke",
                "head_sha": grant.head_sha,
                "grant_id": grant.id,
                "revoke_reason": reason.value,
            },
        }
    )
    await api_client.update_temporary_access_grant(
        grant.id,
        TemporaryAccessGrantUpdate(
            status=TemporaryAccessStatus.REVOKING,
            revoke_reason=reason,
            revoke_run_id=revoke_run_id,
            revoke_attempts=attempts,
        ),
    )
    await redis_client.publish_message(
        DEPLOY_QUEUE,
        DeployMessage(
            task_id=revoke_run_id,
            project_id=grant.project_id,
            user_id="",
            story_id="",
            triggered_by=DeployTrigger.ADMIN,
            action=DeployAction.FEATURE,
            head_sha=grant.head_sha,
            env_overrides={grant.env_key: ""},
        ),
    )
    log.info(
        "temporary_access_revoke_dispatched",
        reason=reason.value,
        revoke_run_id=revoke_run_id,
        attempt=attempts,
        head_sha=grant.head_sha,
    )
    if attempts >= _max_revoke_attempts():
        await notify_admins_best_effort(
            f"Temporary access {grant.env_key} for project {grant.project_id} has survived "
            f"{attempts} revoke attempts (grant {grant.id}); the test identity may still "
            "have access",
            level="error",
            component="temporary_access",
            grant_id=grant.id,
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

    if _revoke_deploy_succeeded(run):
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

    error = _revoke_failure_detail(run)
    await api_client.update_temporary_access_grant(
        grant.id,
        TemporaryAccessGrantUpdate(
            status=TemporaryAccessStatus.REVOKE_FAILED,
            last_error=error,
        ),
    )
    log.error(
        "temporary_access_revoke_failed",
        revoke_run_id=run.id,
        attempts=grant.revoke_attempts,
        error=error,
    )
    await _fail_qa_run_on_unrevoked_access(api_client, grant, error, log)
    counts["revoke_failed"] += 1


def _revoke_deploy_succeeded(run: RunDTO) -> bool:
    """Only a completed deploy that reported SUCCESS actually cleared the value."""
    if run.status is not RunStatus.COMPLETED or run.result is None:
        return False
    return run.result.deploy_outcome is DeployOutcome.SUCCESS


def _revoke_failure_detail(run: RunDTO) -> str:
    outcome = run.result.deploy_outcome.value if run.result is not None else "no result"
    return f"revoke deploy {run.id} ended {run.status.value} ({outcome})"


async def _fail_qa_run_on_unrevoked_access(
    api_client: SchedulerAPIClient,
    grant: TemporaryAccessGrantDTO,
    error: str,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Access that could not be taken back is a failure of the run that took it.

    It is not a reason to stop the scheduler and not something to leave as a log
    line next to a successful run: the QA run that borrowed the identity carries
    the failure, with the grant named, and the grant stays live for the next
    sweep to retry.
    """
    if grant.revoke_reason is TemporaryAccessRevokeReason.RUN_MISSING:
        # There is no run left to carry the failure; the grant itself is the
        # only record, and it already holds the error.
        log.warning("temporary_access_failure_has_no_run", grant_id=grant.id)
        return

    blocker = QABlocker(
        category=QABlockerCategory.QA_CLEANUP_FAILED,
        attempted=f"revoke temporary access {grant.env_key} for project {grant.project_id}",
        sent=f"deploy of {grant.head_sha} with {grant.env_key} cleared",
        received=error,
    )
    await api_client.update_run(
        grant.qa_run_id,
        {
            "status": RunStatus.FAILED.value,
            "error_message": f"temporary access {grant.env_key} is still granted: {error}",
            "result": QARunResult(
                qa_outcome=QAOutcome.BLOCKED,
                summary="temporary test access could not be revoked",
                blocker=blocker,
            ).model_dump(mode="json"),
        },
    )
    log.warning("temporary_access_qa_run_failed", grant_id=grant.id)

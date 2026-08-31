"""Durable reconciliation of temporary QA access through generated-service capabilities."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from http import HTTPStatus
import uuid

import httpx
import structlog

from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import QABlocker, QABlockerCategory, QARunResult
from shared.contracts.dto.temporary_access import (
    TemporaryAccessGrantCreate,
    TemporaryAccessGrantDTO,
    TemporaryAccessGrantUpdate,
    TemporaryAccessRevokeReason,
    TemporaryAccessStatus,
)
from shared.contracts.queues.deploy import DeployAction, DeployMessage, DeployOutcome, DeployTrigger
from shared.contracts.queues.qa import QAMessage, QAOutcome
from shared.notifications import notify_admins_best_effort
from shared.queues import DEPLOY_QUEUE, QA_QUEUE
from shared.redis_client import RedisStreamClient

from .. import startup

logger = structlog.get_logger(__name__)
TERMINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})


def _counts() -> dict[str, int]:
    return {
        "dispatched": 0,
        "released": 0,
        "revoked": 0,
        "expired": 0,
        "revoke_failed": 0,
        "escalated": 0,
    }


def _ttl_minutes() -> int:
    return startup.get_config().get_int("supervisor.temporary_access_ttl_minutes")


def _grant_stale_minutes() -> int:
    return startup.get_config().get_int("supervisor.temporary_access_grant_stale_minutes")


def _max_grant_attempts() -> int:
    return startup.get_config().get_int("supervisor.temporary_access_max_grant_attempts")


def _revoke_stale_minutes() -> int:
    return startup.get_config().get_int("supervisor.temporary_access_revoke_stale_minutes")


def _max_revoke_attempts() -> int:
    return startup.get_config().get_int("supervisor.temporary_access_max_revoke_attempts")


def _unrevoked_ttl_minutes() -> int:
    return startup.get_config().get_int("supervisor.temporary_access_unrevoked_ttl_minutes")


def _new_operation_run_id(operation: str) -> str:
    return f"temporary-access-{operation}-{uuid.uuid4().hex[:12]}"


async def grant_temporary_access(
    api_client,
    redis_client: RedisStreamClient,
    *,
    project_id: str,
    target_application_id: int,
    target_base_url: str,
    head_sha: str,
    qa_message: QAMessage,
) -> TemporaryAccessGrantDTO | None:
    """Store the immutable QA identity and exact target before dispatching grant proof."""
    from shared.contracts.bot_access import QA_TEST_TELEGRAM_ID

    grant_id = f"tempaccess-{qa_message.run_id}"[:255]
    try:
        grant = await api_client.create_temporary_access_grant(
            TemporaryAccessGrantCreate(
                id=grant_id,
                project_id=project_id,
                channel="telegram",
                external_id=str(QA_TEST_TELEGRAM_ID),
                target_application_id=target_application_id,
                target_base_url=target_base_url,
                head_sha=head_sha,
                qa_run_id=qa_message.run_id,
                grant_run_id=_new_operation_run_id("grant"),
                qa_message=qa_message,
            )
        )
    except httpx.HTTPStatusError as error:
        if error.response.status_code != HTTPStatus.CONFLICT:
            raise
        # A target holder or live legacy row is a precondition for this handoff,
        # never a reason to abort the dispatcher cycle that can settle others.
        logger.warning(
            "temporary_access_handoff_deferred",
            grant_id=grant_id,
            project_id=project_id,
            target_application_id=target_application_id,
            qa_run_id=qa_message.run_id,
            remediation=(
                "drain any live legacy grant with the prior release and wait for target cleanup"
            ),
        )
        return None
    if grant.status is not TemporaryAccessStatus.GRANTING:
        return grant
    if await api_client.get_run_if_missing_returns_none(grant.grant_run_id) is None:
        await _publish_operation(api_client, redis_client, grant, grant.grant_run_id, "grant")
    return grant


async def supervise_temporary_access(api_client, redis_client: RedisStreamClient) -> dict[str, int]:
    """Resume every stored lifecycle without selecting a newer target."""
    counts = _counts()
    try:
        grants = await api_client.list_temporary_access_grants_under_watch()
    except httpx.HTTPStatusError as error:
        if error.response.status_code != HTTPStatus.CONFLICT:
            raise
        logger.warning(
            "temporary_access_sweep_deferred_by_precondition",
            remediation="drain any live legacy grant with the prior release",
        )
        return counts
    for grant in grants:
        log = logger.bind(
            grant_id=grant.id,
            qa_run_id=grant.qa_run_id,
            target=grant.target_application_id,
        )
        try:
            if grant.status is TemporaryAccessStatus.GRANTING:
                await _settle_grant(api_client, redis_client, grant, counts, log)
            elif grant.status is TemporaryAccessStatus.REVOKING:
                await _settle_revoke(api_client, redis_client, grant, counts, log)
            elif grant.status is TemporaryAccessStatus.REVOKE_FAILED:
                await _settle_revoke_failed(api_client, redis_client, grant, counts, log)
            else:
                await _settle_granted(api_client, redis_client, grant, counts, log)
        except Exception:
            log.exception("temporary_access_reconciliation_failed")
            counts["revoke_failed"] += 1
    return counts


async def _publish_operation(api_client, redis_client, grant, run_id: str, operation: str) -> None:
    await api_client.create_run(
        {
            "id": run_id,
            "type": RunType.DEPLOY.value,
            "project_id": grant.project_id,
            "status": RunStatus.QUEUED.value,
            "run_metadata": {
                "head_sha": grant.head_sha,
                "temporary_access_grant_id": grant.id,
                "temporary_access_operation": operation,
            },
        }
    )
    await redis_client.publish_message(
        DEPLOY_QUEUE,
        DeployMessage(
            task_id=run_id,
            project_id=grant.project_id,
            unaddressed_reason="temporary QA capability operation",
            story_id="",
            triggered_by=DeployTrigger.ADMIN,
            action=DeployAction.FEATURE,
            head_sha=grant.head_sha,
        ),
    )


def _operation_succeeded(run) -> bool:
    return (
        run.status is RunStatus.COMPLETED
        and run.result is not None
        and run.result.deploy_outcome is DeployOutcome.SUCCESS
    )


def _age_minutes(moment: datetime) -> float:
    reference = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return (datetime.now(UTC) - reference).total_seconds() / 60


async def _settle_grant(api_client, redis_client, grant, counts, log) -> None:
    if datetime.now(UTC) - grant.granted_at >= timedelta(minutes=_ttl_minutes()):
        await _fail_and_revoke(api_client, redis_client, grant, "grant proof expired", counts, log)
        return
    run = await api_client.get_run_if_missing_returns_none(grant.grant_run_id)
    if run is None:
        await _retry_grant_operation(
            api_client, redis_client, grant, "grant capability operation is missing", counts, log
        )
        return
    if run.status not in TERMINAL_RUN_STATUSES:
        if _age_minutes(run.created_at) >= _grant_stale_minutes():
            await _retry_grant_operation(
                api_client,
                redis_client,
                grant,
                "grant capability operation is stale",
                counts,
                log,
            )
        return
    if not _operation_succeeded(run):
        await _retry_grant_operation(
            api_client, redis_client, grant, "grant proof failed", counts, log
        )
        return
    granted = await api_client.update_temporary_access_grant(
        grant.id, TemporaryAccessGrantUpdate(status=TemporaryAccessStatus.GRANTED)
    )
    await _settle_granted(api_client, redis_client, granted, counts, log)


async def _retry_grant_operation(api_client, redis_client, grant, detail, counts, log) -> None:
    """Retry a lost, stale, or failed grant against the record's fixed target."""
    if grant.grant_attempts >= _max_grant_attempts():
        log.error(
            "temporary_access_grant_attempts_exhausted",
            attempts=grant.grant_attempts,
            error=detail,
        )
        await _fail_and_revoke(api_client, redis_client, grant, detail, counts, log)
        return
    run_id = _new_operation_run_id("grant")
    await api_client.update_temporary_access_grant(
        grant.id,
        TemporaryAccessGrantUpdate(
            grant_run_id=run_id,
            grant_attempts=grant.grant_attempts + 1,
            last_error=detail,
        ),
    )
    await _publish_operation(api_client, redis_client, grant, run_id, "grant")
    counts["dispatched"] += 1


async def _release_qa(api_client, redis_client, grant, counts) -> None:
    if grant.qa_dispatched_at is not None:
        return
    await redis_client.publish_message(QA_QUEUE, grant.qa_message)
    await api_client.update_temporary_access_grant(
        grant.id, TemporaryAccessGrantUpdate(qa_dispatched=True)
    )
    counts["released"] += 1


async def _settle_granted(api_client, redis_client, grant, counts, log) -> None:
    run = await api_client.get_run_if_missing_returns_none(grant.qa_run_id)
    reason = None
    if run is None:
        reason = TemporaryAccessRevokeReason.RUN_MISSING
    elif run.status in TERMINAL_RUN_STATUSES:
        reason = TemporaryAccessRevokeReason.RUN_TERMINAL
    elif datetime.now(UTC) - grant.granted_at >= timedelta(minutes=_ttl_minutes()):
        reason = TemporaryAccessRevokeReason.EXPIRED
        counts["expired"] += 1
    if reason is None:
        await _release_qa(api_client, redis_client, grant, counts)
        return
    await _dispatch_revoke(api_client, redis_client, grant, reason)
    counts["dispatched"] += 1


async def _fail_and_revoke(api_client, redis_client, grant, detail, counts, log) -> None:
    await _fail_qa_run(api_client, grant, detail)
    await _dispatch_revoke(
        api_client, redis_client, grant, TemporaryAccessRevokeReason.GRANT_FAILED
    )
    counts["dispatched"] += 1


async def _dispatch_revoke(api_client, redis_client, grant, reason) -> None:
    run_id = _new_operation_run_id("revoke")
    # Once cleanup starts, its recorded reason is immutable. A later terminal
    # QA state must not relabel a grant-failure cleanup as a run-terminal one.
    recorded_reason = grant.revoke_reason or reason
    await api_client.update_temporary_access_grant(
        grant.id,
        TemporaryAccessGrantUpdate(
            status=TemporaryAccessStatus.REVOKING,
            revoke_reason=recorded_reason,
            revoke_run_id=run_id,
            revoke_attempts=grant.revoke_attempts + 1,
        ),
    )
    await _publish_operation(api_client, redis_client, grant, run_id, "revoke")


async def _settle_revoke(api_client, redis_client, grant, counts, log) -> None:
    if grant.revoke_run_id is None or grant.revoke_reason is None:
        raise ValueError("revoking grant has no operation or reason")
    run = await api_client.get_run_if_missing_returns_none(grant.revoke_run_id)
    if run is None:
        await _record_revoke_failure(
            api_client,
            redis_client,
            grant,
            "revoke capability operation is missing",
            counts,
            log,
        )
        return
    if run.status not in TERMINAL_RUN_STATUSES:
        if _age_minutes(run.created_at) >= _revoke_stale_minutes():
            await _record_revoke_failure(
                api_client,
                redis_client,
                grant,
                "revoke capability operation is stale",
                counts,
                log,
            )
        return
    if _operation_succeeded(run):
        await api_client.update_temporary_access_grant(
            grant.id, TemporaryAccessGrantUpdate(status=TemporaryAccessStatus.REVOKED)
        )
        counts["revoked"] += 1
        return
    await _record_revoke_failure(
        api_client, redis_client, grant, "revoke proof failed", counts, log
    )


def _revoke_retries_are_spent(grant) -> bool:
    return (
        grant.revoke_attempts >= _max_revoke_attempts()
        or _age_minutes(grant.granted_at) >= _unrevoked_ttl_minutes()
    )


async def _settle_revoke_failed(api_client, redis_client, grant, counts, log) -> None:
    """Resume a retry that was recorded before its replacement run was published."""
    if grant.escalated_at is not None:
        return
    if grant.revoke_reason is None:
        raise ValueError("failed revoke has no reason")
    if _revoke_retries_are_spent(grant):
        await _escalate_unrevoked(
            api_client, grant, grant.last_error or "revoke proof failed", counts
        )
        return
    await _dispatch_revoke(api_client, redis_client, grant, grant.revoke_reason)
    counts["dispatched"] += 1


async def _record_revoke_failure(api_client, redis_client, grant, detail, counts, log) -> None:
    """Leave failed cleanup durable, bounded, and visible to an administrator once."""
    counts["revoke_failed"] += 1
    if grant.escalated_at is not None:
        return
    if _revoke_retries_are_spent(grant):
        await _escalate_unrevoked(api_client, grant, detail, counts)
        return
    await api_client.update_temporary_access_grant(
        grant.id,
        TemporaryAccessGrantUpdate(
            status=TemporaryAccessStatus.REVOKE_FAILED,
            last_error=detail,
        ),
    )
    await _dispatch_revoke(api_client, redis_client, grant, grant.revoke_reason)
    counts["dispatched"] += 1
    log.warning(
        "temporary_access_revoke_retry_dispatched",
        revoke_run_id=grant.revoke_run_id,
        attempts=grant.revoke_attempts,
        error=detail,
    )


async def _escalate_unrevoked(api_client, grant, detail, counts) -> None:
    """Persist the once-only escalation before issuing its best-effort alert."""
    await api_client.escalate_temporary_access_grant(
        grant.id,
        error=detail,
        run_error_message="temporary QA access could not be revoked",
        run_result=QARunResult(
            qa_outcome=QAOutcome.BLOCKED,
            summary="temporary QA access could not be revoked",
            blocker=QABlocker(
                category=QABlockerCategory.QA_CLEANUP_FAILED,
                attempted="revoke temporary QA access",
                sent="capability revoke",
                received="inactive readback was not proved",
            ),
        ),
    )
    await notify_admins_best_effort(
        "Temporary QA access could not be revoked and needs operator cleanup: "
        f"grant {grant.id}, project {grant.project_id}, QA run {grant.qa_run_id}. "
        f"Last error: {detail}",
        level="error",
        component="temporary_access",
        grant_id=grant.id,
        qa_run_id=grant.qa_run_id,
    )
    counts["escalated"] += 1


async def _fail_qa_run(api_client, grant, detail: str) -> None:
    await api_client.record_run_outcome_unless_settled(
        grant.qa_run_id,
        {
            "status": RunStatus.FAILED.value,
            "error_message": detail,
            "result": QARunResult(
                qa_outcome=QAOutcome.BLOCKED,
                summary="temporary QA access could not be granted",
                blocker=QABlocker(
                    category=QABlockerCategory.QA_ACCESS_GRANT_FAILED,
                    attempted="grant temporary QA access",
                    sent="capability grant",
                    received=detail,
                ),
            ).model_dump(mode="json"),
        },
    )

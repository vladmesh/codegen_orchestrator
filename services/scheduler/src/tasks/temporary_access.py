"""Durable reconciliation of temporary QA access through generated-service capabilities."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

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


def _max_revoke_attempts() -> int:
    return startup.get_config().get_int("supervisor.temporary_access_max_revoke_attempts")


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

    grant = await api_client.create_temporary_access_grant(
        TemporaryAccessGrantCreate(
            id=f"tempaccess-{qa_message.run_id}"[:255],
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
    if grant.status is not TemporaryAccessStatus.GRANTING:
        return grant
    if await api_client.get_run_if_missing_returns_none(grant.grant_run_id) is None:
        await _publish_operation(api_client, redis_client, grant, grant.grant_run_id, "grant")
    return grant


async def supervise_temporary_access(api_client, redis_client: RedisStreamClient) -> dict[str, int]:
    """Resume every stored lifecycle without selecting a newer target."""
    counts = _counts()
    for grant in await api_client.list_temporary_access_grants_under_watch():
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


async def _settle_grant(api_client, redis_client, grant, counts, log) -> None:
    if datetime.now(UTC) - grant.granted_at >= timedelta(minutes=_ttl_minutes()):
        await _fail_and_revoke(api_client, redis_client, grant, "grant proof expired", counts, log)
        return
    run = await api_client.get_run_if_missing_returns_none(grant.grant_run_id)
    if run is None:
        run_id = _new_operation_run_id("grant")
        await api_client.update_temporary_access_grant(
            grant.id, TemporaryAccessGrantUpdate(grant_run_id=run_id)
        )
        await _publish_operation(api_client, redis_client, grant, run_id, "grant")
        counts["dispatched"] += 1
        return
    if run.status not in TERMINAL_RUN_STATUSES:
        return
    if not _operation_succeeded(run):
        await _fail_and_revoke(api_client, redis_client, grant, "grant proof failed", counts, log)
        return
    granted = await api_client.update_temporary_access_grant(
        grant.id, TemporaryAccessGrantUpdate(status=TemporaryAccessStatus.GRANTED)
    )
    await _settle_granted(api_client, redis_client, granted, counts, log)


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
    await api_client.update_temporary_access_grant(
        grant.id,
        TemporaryAccessGrantUpdate(
            status=TemporaryAccessStatus.REVOKING,
            revoke_reason=reason,
            revoke_run_id=run_id,
        ),
    )
    await _publish_operation(api_client, redis_client, grant, run_id, "revoke")


async def _settle_revoke(api_client, redis_client, grant, counts, log) -> None:
    if grant.revoke_run_id is None or grant.revoke_reason is None:
        raise ValueError("revoking grant has no operation or reason")
    run = await api_client.get_run_if_missing_returns_none(grant.revoke_run_id)
    if run is None:
        await _publish_operation(api_client, redis_client, grant, grant.revoke_run_id, "revoke")
        counts["dispatched"] += 1
        return
    if run.status not in TERMINAL_RUN_STATUSES:
        return
    if _operation_succeeded(run):
        await api_client.update_temporary_access_grant(
            grant.id, TemporaryAccessGrantUpdate(status=TemporaryAccessStatus.REVOKED)
        )
        counts["revoked"] += 1
        return
    attempts = grant.revoke_attempts + 1
    if attempts >= _max_revoke_attempts():
        await _fail_qa_run(api_client, grant, "revoke proof failed")
        await api_client.escalate_temporary_access_grant(
            grant.id,
            error="revoke proof failed",
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
        counts["escalated"] += 1
        return
    await api_client.update_temporary_access_grant(
        grant.id,
        TemporaryAccessGrantUpdate(
            status=TemporaryAccessStatus.REVOKE_FAILED,
            revoke_attempts=attempts,
            last_error="revoke proof failed",
        ),
    )
    await _dispatch_revoke(api_client, redis_client, grant, grant.revoke_reason)
    counts["revoke_failed"] += 1
    counts["dispatched"] += 1


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

"""Deploy result handlers — pure run.status/result updates, no story transitions.

Story lifecycle (DEPLOYING → TESTING/FAILED) is managed by the dispatcher's
supervise_deploying_stories(), which reads run.result.deploy_outcome.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from shared.contracts.bot_access import TEST_IDENTITY_ENV_KEY
from shared.contracts.dto.project import ProjectDTO
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.run_result import DeployRunResult
from shared.contracts.dto.users_grant import (
    GrantIntent,
    GrantIntentKind,
    GrantIntentStatus,
)
from shared.contracts.env_contract import CanonicalEnvContract
from shared.contracts.queues.deploy import DeployMessage, DeployOutcome
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ..clients.users_grant import GeneratedServiceGrantClient
from ._events import publish_callback_event
from ._live_work import live_work_settled, live_work_unsettled

logger = structlog.get_logger(__name__)

_USERS_GRANT_CAPABILITY = "USERS_GRANT_CAPABILITY"


def _declares_test_identity_slot(result: dict) -> bool:
    """Whether the commit that was just deployed has a test identity slot.

    Read from the contract this deploy resolved, so the answer describes the
    running code rather than whatever the branch declares now. A deploy that
    skipped contract resolution reports no slot: nothing may deploy a value the
    generated repository has not declared.
    """
    contract = result.get("environment_contract")
    if contract is None:
        return False
    return TEST_IDENTITY_ENV_KEY in CanonicalEnvContract.model_validate(contract).entries


async def _handle_smoke_failure(
    *,
    result: dict,
    smoke_result: dict,
    task_id: str,
    project_id: str,
    project_name: str,
    callback_stream: str,
    telegram_chat_id: str,
    story_id: str,
    redis: RedisStreamClient,
    msg: DeployMessage,
) -> dict:
    """Handle deploy success with smoke test failure.

    Stores a deterministic retry outcome for the dispatcher to act on.
    """
    smoke_details = "; ".join(
        f"{c['module']}: {c['detail']}"
        for c in smoke_result.get("checks", [])
        if c.get("result") == "fail"
    )
    error_msg = f"Deployed but smoke test failed: {smoke_details}"
    logger.warning(
        "deploy_job_smoke_failed",
        task_id=task_id,
        deployed_url=result["deployed_url"],
        smoke_details=smoke_details,
    )

    run_result = DeployRunResult(
        deploy_outcome=DeployOutcome.RETRY,
        deployed_url=result["deployed_url"],
        deployment_result=result.get("deployment_result"),
        smoke_result=smoke_result,
        error_details=smoke_details,
        deploy_fix_attempt=msg.deploy_fix_attempt,
    )
    await api_client.patch(
        f"runs/{task_id}",
        json={
            "status": RunStatus.FAILED.value,
            "error_message": error_msg,
            "result": run_result.model_dump(mode="json"),
        },
    )

    await publish_callback_event(
        redis,
        callback_stream,
        "failed",
        task_id,
        error_msg,
        telegram_chat_id=telegram_chat_id,
        project_id=project_id,
    )

    return live_work_unsettled(
        {
            "status": "failed",
            "error": error_msg,
            "deployed_url": result["deployed_url"],
            "finished_at": datetime.now(UTC).isoformat(),
        }
    )


async def _handle_deploy_success(  # noqa: PLR0913
    *,
    result: dict,
    smoke_result: dict | None,
    task_id: str,
    project_id: str,
    project: ProjectDTO,
    callback_stream: str,
    telegram_chat_id: str,
    story_id: str,
    redis: RedisStreamClient,
    application_id: int | None = None,
    grant_intent: GrantIntent | None = None,
) -> dict:
    """Handle successful deploy — update run, no story transitions.

    Stores deploy_outcome=success with deployed_url and application_id
    so dispatcher can hand off to QA.
    """
    if grant_intent is not None:
        failure = await _apply_grant_intent(
            task_id=task_id,
            project_id=project_id,
            deployed_url=result["deployed_url"],
            application_id=application_id,
            secret_values=result.get("secret_values", {}),
            intent=grant_intent,
        )
        if failure is not None:
            return await _handle_owner_access_failure(
                result=result,
                task_id=task_id,
                project_id=project_id,
                callback_stream=callback_stream,
                telegram_chat_id=telegram_chat_id,
                redis=redis,
                reason=failure,
                application_id=application_id,
            )

    logger.info(
        "deploy_job_success",
        task_id=task_id,
        deployed_url=result["deployed_url"],
    )
    run_result = DeployRunResult(
        deploy_outcome=DeployOutcome.SUCCESS,
        deployed_url=result["deployed_url"],
        deployment_result=result.get("deployment_result"),
        smoke_result=smoke_result,
        application_id=application_id,
        bot_username=result.get("bot_username"),
        test_identity_slot=_declares_test_identity_slot(result),
    )
    await api_client.patch(
        f"runs/{task_id}",
        json={
            "status": RunStatus.COMPLETED.value,
            "result": run_result.model_dump(mode="json"),
        },
    )

    # Callback for standalone deploys (no story)
    if not story_id:
        await publish_callback_event(
            redis,
            callback_stream,
            "completed",
            task_id,
            f"Deploy completed: {result['deployed_url']}",
            telegram_chat_id=telegram_chat_id,
            project_id=project_id,
        )

    return live_work_settled(
        {
            "status": "success",
            "deployed_url": result["deployed_url"],
            "finished_at": datetime.now(UTC).isoformat(),
        }
    )


async def _apply_grant_intent(  # noqa: PLR0913
    *,
    task_id: str,
    project_id: str,
    deployed_url: str,
    application_id: int | None,
    secret_values: dict,
    intent: GrantIntent,
) -> str | None:
    """Execute exactly one durable intent after deploy health checks.

    The capability comes only from this deploy's in-memory resolver output.
    Every persisted or logged result below is a bounded diagnostic, never the
    capability or a decrypted project secret.
    """
    if intent.status is GrantIntentStatus.APPLIED:
        return None
    if intent.target_application_id is not None and intent.target_application_id != application_id:
        await api_client.complete_users_grant_intent(
            project_id,
            intent.id,
            execution_run_id=task_id,
            active=False,
            detail="target_application_mismatch",
        )
        return "target_application_mismatch"
    capability = secret_values.get(_USERS_GRANT_CAPABILITY)
    if not isinstance(capability, str) or not capability:
        await api_client.complete_users_grant_intent(
            project_id,
            intent.id,
            execution_run_id=task_id,
            active=False,
            detail="capability_unavailable",
        )
        return "capability_unavailable"
    proof = await GeneratedServiceGrantClient(deployed_url).grant_and_resolve(
        channel=intent.channel, external_id=intent.external_id, capability=capability
    )
    if not proof.active:
        safe_failure = proof.failure.value if proof.failure is not None else "unverified"
        await api_client.complete_users_grant_intent(
            project_id, intent.id, execution_run_id=task_id, active=False, detail=safe_failure
        )
        return safe_failure
    try:
        await api_client.complete_users_grant_intent(
            project_id, intent.id, execution_run_id=task_id, active=True
        )
    except Exception:
        if intent.kind is GrantIntentKind.INCOMING_OWNER:
            return "ownership_apply_failed"
        return "intent_apply_failed"
    return None


async def _handle_owner_access_failure(
    *,
    result: dict,
    task_id: str,
    project_id: str,
    callback_stream: str,
    telegram_chat_id: str,
    redis: RedisStreamClient,
    reason: str,
    application_id: int | None,
) -> dict:
    """Keep a grant/readback failure retryable without disclosing credentials."""
    error_msg = f"Deployed service did not verify permanent access: {reason}"
    logger.warning("deploy_grant_intent_failed", task_id=task_id, reason=reason)
    await api_client.patch(
        f"runs/{task_id}",
        json={
            "status": RunStatus.FAILED.value,
            "error_message": error_msg,
            "result": DeployRunResult(
                deploy_outcome=DeployOutcome.OWNER_ACCESS_PROOF_FAILED,
                deployed_url=result["deployed_url"],
                deployment_result=result.get("deployment_result"),
                smoke_result=result.get("smoke_result"),
                application_id=application_id,
                bot_username=result.get("bot_username"),
                test_identity_slot=_declares_test_identity_slot(result),
                error_details=reason,
            ).model_dump(mode="json"),
        },
    )
    await publish_callback_event(
        redis,
        callback_stream,
        "failed",
        task_id,
        error_msg,
        telegram_chat_id=telegram_chat_id,
        project_id=project_id,
    )
    return live_work_unsettled(
        {"status": "failed", "error": error_msg, "deployed_url": result["deployed_url"]}
    )

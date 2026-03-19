"""Deploy result handlers — pure run.status/result updates, no story transitions.

Story lifecycle (DEPLOYING → TESTING/FAILED) is managed by the dispatcher's
supervise_deploying_stories(), which reads run.result.deploy_outcome.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from shared.contracts.dto.project import ProjectDTO
from shared.contracts.dto.run import RunStatus
from shared.contracts.queues.deploy import DeployMessage, DeployOutcome
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ._events import publish_callback_event
from .deploy_failure_handler import _classify_deploy_failure

logger = structlog.get_logger(__name__)


async def _handle_smoke_failure(
    *,
    result: dict,
    smoke_result: dict,
    task_id: str,
    project_id: str,
    project_name: str,
    callback_stream: str,
    user_id: str,
    story_id: str,
    redis: RedisStreamClient,
    msg: DeployMessage,
) -> dict:
    """Handle deploy success with smoke test failure.

    Classifies the failure and stores the classification in run.result
    for the dispatcher to act on.
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

    # Classify failure for dispatcher routing
    classification = await _classify_deploy_failure(smoke_details)
    deploy_outcome = {
        "CODE_FIX": DeployOutcome.CODE_FIX,
        "RETRY": DeployOutcome.RETRY,
        "GIVE_UP": DeployOutcome.GIVE_UP,
    }.get(classification, DeployOutcome.RETRY)

    await api_client.patch(
        f"runs/{task_id}",
        json={
            "status": RunStatus.FAILED.value,
            "error_message": error_msg,
            "result": {
                "deploy_outcome": deploy_outcome.value,
                "deployed_url": result["deployed_url"],
                "deployment_result": result.get("deployment_result"),
                "smoke_result": smoke_result,
                "error_details": smoke_details,
                "deploy_fix_attempt": msg.deploy_fix_attempt,
            },
        },
    )

    await publish_callback_event(
        redis,
        callback_stream,
        "failed",
        task_id,
        error_msg,
        user_id=user_id,
        project_id=project_id,
    )

    return {
        "status": "failed",
        "error": error_msg,
        "deployed_url": result["deployed_url"],
        "finished_at": datetime.now(UTC).isoformat(),
    }


async def _handle_deploy_success(
    *,
    result: dict,
    smoke_result: dict | None,
    task_id: str,
    project_id: str,
    project: ProjectDTO,
    callback_stream: str,
    user_id: str,
    story_id: str,
    redis: RedisStreamClient,
    application_id: int | None = None,
) -> dict:
    """Handle successful deploy — update run, no story transitions.

    Stores deploy_outcome=success with deployed_url and application_id
    so dispatcher can hand off to QA.
    """
    logger.info(
        "deploy_job_success",
        task_id=task_id,
        deployed_url=result["deployed_url"],
    )
    await api_client.patch(
        f"runs/{task_id}",
        json={
            "status": RunStatus.COMPLETED.value,
            "result": {
                "deploy_outcome": DeployOutcome.SUCCESS.value,
                "deployed_url": result["deployed_url"],
                "deployment_result": result.get("deployment_result"),
                "smoke_result": smoke_result,
                "application_id": application_id,
                "bot_username": result.get("bot_username"),
            },
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
            user_id=user_id,
            project_id=project_id,
        )

    return {
        "status": "success",
        "deployed_url": result["deployed_url"],
        "finished_at": datetime.now(UTC).isoformat(),
    }

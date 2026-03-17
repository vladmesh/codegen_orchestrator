"""Deploy result handlers for success and smoke-test failure outcomes."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from shared.contracts.dto.project import ProjectDTO
from shared.contracts.queues.deploy import DeployMessage
from shared.contracts.queues.qa import QAMessage
from shared.queues import QA_QUEUE
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ._events import publish_callback_event
from .deploy_failure_handler import (
    _classify_deploy_failure,
    _route_deploy_failure,
    _track_deploy_retry,
    _transition_story_safe,
)

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
    """Handle deploy success with smoke test failure."""
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
    await api_client.patch(
        f"runs/{task_id}",
        json={
            "status": "failed",
            "error_message": error_msg,
            "result": {
                "deployed_url": result["deployed_url"],
                "deployment_result": result.get("deployment_result"),
                "smoke_result": smoke_result,
            },
        },
    )
    # Classify and route failure
    classification = await _classify_deploy_failure(smoke_details)
    await _route_deploy_failure(
        classification=classification,
        redis=redis,
        msg=msg,
        error_details=smoke_details,
        story_id=story_id,
    )
    # For RETRY, also track via retry counter
    if classification == "RETRY":
        await _track_deploy_retry(redis=redis, story_id=story_id)

    await publish_callback_event(
        redis,
        callback_stream,
        "failed",
        task_id,
        error_msg,
        user_id=user_id,
        project_id=project_id,
    )
    # No proactive message — smoke failure is internal (retried or redispatched)

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
) -> dict:
    """Handle successful deploy (with or without smoke)."""
    logger.info(
        "deploy_job_success",
        task_id=task_id,
        deployed_url=result["deployed_url"],
    )
    await api_client.patch(
        f"runs/{task_id}",
        json={
            "status": "completed",
            "result": {
                "deployed_url": result["deployed_url"],
                "deployment_result": result.get("deployment_result"),
                "smoke_result": smoke_result,
            },
        },
    )
    # Hand off to QA if story exists, otherwise complete directly
    if story_id:
        await _transition_story_safe(story_id, "test")
        await redis.publish_message(
            QA_QUEUE,
            QAMessage(
                story_id=story_id,
                project_id=project_id,
                user_id=user_id,
                deployed_url=result["deployed_url"],
            ),
        )
        logger.info("qa_handoff", story_id=story_id, deployed_url=result["deployed_url"])
        # Worker container NOT deleted — QA may need it for fix tasks
    else:
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

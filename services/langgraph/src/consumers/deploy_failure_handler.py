"""Deploy failure result updates.

Story lifecycle routing (CODE_FIX → engineering, RETRY → redeploy, GIVE_UP → fail)
is managed by the dispatcher's supervise_deploying_stories().
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.run_result import DeployRunResult, MissingUserSecret
from shared.contracts.queues.deploy import DeployOutcome
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ._events import publish_callback_event
from ._live_work import live_work_unsettled

logger = structlog.get_logger(__name__)


async def _handle_deploy_failure(
    *,
    task_id: str,
    project_id: str,
    error_msg: str,
    story_id: str,
    callback_stream: str,
    user_id: str,
    redis: RedisStreamClient,
    deploy_outcome: DeployOutcome = DeployOutcome.RETRY,
    deploy_fix_attempt: int = 0,
    missing_user_secrets: list[MissingUserSecret] | None = None,
) -> dict:
    """Update run status/result on deploy failure.

    Stores deploy_outcome and error_details in run.result for
    the dispatcher to read and route story lifecycle. On a
    WAITING_FOR_USER_SECRET outcome, `missing_user_secrets` carries the
    structured keys the scheduler asks the user for.
    """
    run_result = DeployRunResult(
        deploy_outcome=deploy_outcome,
        error_details=error_msg,
        deploy_fix_attempt=deploy_fix_attempt,
        missing_user_secrets=missing_user_secrets or [],
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
        user_id=user_id,
        project_id=project_id or "",
    )

    return live_work_unsettled(
        {
            "status": "failed",
            "error": error_msg,
            "finished_at": datetime.now(UTC).isoformat(),
        }
    )

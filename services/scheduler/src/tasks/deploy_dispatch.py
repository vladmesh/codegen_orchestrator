"""Canonical scheduler construction and publication of deploy attempts."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from shared.contracts.dto.run import RunType
from shared.contracts.queues.deploy import DeployAction, DeployMessage, DeployTrigger
from shared.queues import DEPLOY_QUEUE

from ._recipients import Recipient

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient
    from shared.redis import RedisStreamClient


def deploy_run_id(prefix: str, *identity_parts: str) -> str:
    """Return one stable Run id for one logical deploy handoff."""
    canonical = "\x1f".join(identity_parts)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return f"{prefix}-{digest}"


async def dispatch_deploy(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    *,
    run_id: str,
    project_id: str,
    story_id: str,
    recipient: Recipient,
    action: DeployAction,
    head_sha: str,
    deployed_commit_sha: str,
    run_metadata: dict[str, Any],
    transition_action: str | None = None,
    triggered_by: DeployTrigger = DeployTrigger.WEBHOOK,
) -> DeployMessage:
    """Persist the canonical deploy Run before moving the Story and publishing work.

    The Run id is supplied by the caller from stable attempt identity. Repeating a
    handoff therefore reuses the same Run instead of minting a second attempt.
    When a Story transition is part of the handoff, the Run is guaranteed to
    exist first: a failed Run create can no longer leave the Story in DEPLOYING
    with no current deploy evidence.
    """
    await api_client.create_run_if_absent(
        {
            "id": run_id,
            "type": RunType.DEPLOY.value,
            "project_id": project_id,
            "story_id": story_id,
            "run_metadata": run_metadata,
        }
    )
    if transition_action is not None:
        await api_client.transition_story(story_id, transition_action)

    message = DeployMessage(
        task_id=run_id,
        project_id=project_id,
        telegram_chat_id=recipient.telegram_chat_id,
        unaddressed_reason=recipient.unaddressed_reason,
        story_id=story_id,
        triggered_by=triggered_by,
        action=action,
        head_sha=head_sha,
        deployed_commit_sha=deployed_commit_sha,
    )
    await redis_client.publish_message(DEPLOY_QUEUE, message)
    return message

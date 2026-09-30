"""Canonical scheduler construction, publication, and recovery of deploy attempts."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from typing import TYPE_CHECKING, Any

from shared.contracts.dto.run import RunType
from shared.contracts.queues.deploy import DeployAction, DeployMessage, DeployTrigger
from shared.queues import DEPLOY_QUEUE

from ._recipients import Recipient

if TYPE_CHECKING:
    from shared.redis import RedisStreamClient

    from ..clients.api import SchedulerAPIClient

DEPLOY_HANDOFF_MESSAGE_KEY = "deploy_handoff_message"
DEPLOY_HANDOFF_DISPATCHED_AT_KEY = "deploy_handoff_dispatched_at"


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
    """Persist one recoverable deploy handoff, then move its Story and publish it.

    The exact queue message is stored on the Run before any Story transition.
    A known publish failure therefore leaves durable evidence the deploying
    supervisor can replay. The caller supplies a stable Run id, so retries land
    on the same attempt rather than manufacturing another Run.
    """
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
    message_data = message.model_dump(mode="json")
    persisted = await api_client.create_run_if_absent(
        {
            "id": run_id,
            "type": RunType.DEPLOY.value,
            "project_id": project_id,
            "story_id": story_id,
            "run_metadata": {
                **run_metadata,
                DEPLOY_HANDOFF_MESSAGE_KEY: message_data,
            },
        }
    )
    persisted_metadata = getattr(persisted, "run_metadata", None)
    if (
        isinstance(persisted_metadata, dict)
        and DEPLOY_HANDOFF_MESSAGE_KEY in persisted_metadata
        and persisted_metadata[DEPLOY_HANDOFF_MESSAGE_KEY] != message_data
    ):
        raise ValueError(f"deploy handoff {run_id} already names a different message")

    if transition_action is not None:
        await api_client.transition_story(story_id, transition_action)

    await redis_client.publish_message(DEPLOY_QUEUE, message)
    await api_client.update_run(
        run_id,
        {
            "run_metadata": {
                DEPLOY_HANDOFF_DISPATCHED_AT_KEY: datetime.now(UTC).isoformat(),
            }
        },
    )
    return message


async def recover_deploy_handoff(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    run: Any,
    *,
    minimum_age_minutes: float,
) -> bool:
    """Replay one queued handoff whose publisher died before recording success."""
    metadata = getattr(run, "run_metadata", None) or {}
    message_data = metadata.get(DEPLOY_HANDOFF_MESSAGE_KEY)
    if message_data is None or metadata.get(DEPLOY_HANDOFF_DISPATCHED_AT_KEY):
        return False

    created_at = getattr(run, "created_at", None)
    if created_at is None:
        return False
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    age_minutes = (datetime.now(UTC) - created_at).total_seconds() / 60
    if age_minutes < minimum_age_minutes:
        return False

    message = DeployMessage.model_validate(message_data)
    await redis_client.publish_message(DEPLOY_QUEUE, message)
    await api_client.update_run(
        run.id,
        {
            "run_metadata": {
                DEPLOY_HANDOFF_DISPATCHED_AT_KEY: datetime.now(UTC).isoformat(),
            }
        },
    )
    return True

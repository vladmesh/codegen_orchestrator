"""Pipeline supervisor — detect stuck stories/tasks, retry or escalate."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.architect import ArchitectMessage
from shared.contracts.queues.po import POSystemEvent, to_flat_fields
from shared.queues import ARCHITECT_QUEUE, PO_INPUT_QUEUE
from shared.redis_client import RedisStreamClient

from .story_completion import _cleanup_story_worker

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient

logger = structlog.get_logger(__name__)

# Supervisor thresholds
STORY_STUCK_THRESHOLD_MINUTES = 5
TASK_STUCK_THRESHOLD_MINUTES = 30
STORY_MAX_ARCHITECT_RETRIES = 3

# Failure reasons that should not be retried by the supervisor
NON_RETRYABLE_REASONS = {"worker_rejected", "ci_infra_failure", "developer_blocked"}

STORY_RETRY_KEY_PREFIX = "story:architect_retries:"
STORY_RETRY_TTL = 3600  # 1 hour — retries expire after this


def _parse_datetime(iso_str: str) -> datetime:
    """Parse ISO datetime string, handling both Z and +00:00 suffixes."""
    if iso_str.endswith("Z"):
        iso_str = iso_str[:-1] + "+00:00"
    return datetime.fromisoformat(iso_str)


async def supervise_stuck_stories(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    *,
    _retry_counts: dict[str, int] | None = None,
) -> dict[str, int]:
    """Detect stories stuck in 'created' with no tasks and retry architect.

    Retry counts are persisted in Redis so they survive scheduler restarts.

    Returns dict with 'retried' and 'failed' counts.
    """
    stories = await api_client.get_stories_by_status(StoryStatus.CREATED)
    retried = 0
    failed = 0

    # Build set of projects that already have an active story
    active_stories = await api_client.get_stories_by_status(StoryStatus.IN_PROGRESS)
    active_projects = {s.get("project_id") for s in active_stories}

    now = datetime.now(UTC)
    redis = redis_client._redis

    for story in stories:
        story_id = story["id"]
        project_id = story.get("project_id")
        created_at = _parse_datetime(story["created_at"])
        age_minutes = (now - created_at).total_seconds() / 60

        if age_minutes < STORY_STUCK_THRESHOLD_MINUTES:
            continue

        # Skip if project already has an active story (sequential processing)
        if project_id in active_projects:
            continue

        # Only retry if architect hasn't created any tasks yet
        tasks = await api_client.get_tasks_by_story(story_id)
        if tasks:
            continue

        log = logger.bind(story_id=story_id, age_minutes=round(age_minutes, 1))

        retry_key = f"{STORY_RETRY_KEY_PREFIX}{story_id}"
        raw = await redis.get(retry_key)
        current_retries = int(raw) if raw else 0

        if current_retries >= STORY_MAX_ARCHITECT_RETRIES:
            log.error(
                "story_terminal_failure",
                reason="architect_retries_exhausted",
                retries=current_retries,
            )
            await api_client.fail_story(story_id)
            await redis.delete(retry_key)
            failed += 1
            continue

        # Retry: republish to architect:queue
        arch_msg = ArchitectMessage(
            story_id=story_id,
            project_id=story.get("project_id", ""),
            user_id=story.get("user_id", ""),
        )
        await redis_client.publish_message(ARCHITECT_QUEUE, arch_msg)
        await redis.set(retry_key, current_retries + 1, ex=STORY_RETRY_TTL)

        log.warning(
            "story_stuck_retry",
            retry_attempt=current_retries + 1,
            max_retries=STORY_MAX_ARCHITECT_RETRIES,
        )
        retried += 1

    return {"retried": retried, "failed": failed}


async def supervise_failed_tasks(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """Detect failed tasks and retry or escalate to story failure.

    Returns dict with 'retried' and 'failed' counts.
    """
    tasks = await api_client.get_tasks_by_status(TaskStatus.FAILED)
    retried = 0
    failed = 0

    for task in tasks:
        task_id = task["id"]
        story_id = task.get("story_id")

        # Skip standalone tasks (not part of a story)
        if not story_id:
            continue

        # Skip non-retryable failures — needs admin intervention, not retry
        failure_reason = (task.get("failure_metadata") or {}).get("failure_reason")
        if failure_reason in NON_RETRYABLE_REASONS:
            continue

        current_iter = task.get("current_iteration", 0)
        max_iter = task.get("max_iterations", 3)
        log = logger.bind(task_id=task_id, story_id=story_id, iteration=current_iter)

        if current_iter < max_iter:
            # Retry: failed → backlog → todo, bump iteration
            await api_client.transition_task(task_id, TaskStatus.BACKLOG, "supervisor")
            await api_client.transition_task(task_id, TaskStatus.TODO, "supervisor")
            await api_client.update_task(task_id, {"current_iteration": current_iter + 1})
            log.warning(
                "task_retry",
                new_iteration=current_iter + 1,
                max_iterations=max_iter,
            )
            retried += 1
        else:
            # Terminal failure — cancel siblings, fail story, notify user
            log.error(
                "task_terminal_failure",
                reason="retries_exhausted",
            )
            siblings = await api_client.get_tasks_by_story(story_id)
            for sibling in siblings:
                sib_status = sibling.get("status", "")
                if sibling["id"] != task_id and sib_status not in (
                    TaskStatus.DONE,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                ):
                    await api_client.transition_task(
                        sibling["id"],
                        TaskStatus.CANCELLED,
                        "supervisor",
                    )

            try:
                await api_client.fail_story(story_id)
            except Exception:
                log.warning("fail_story_transition_failed", story_id=story_id, exc_info=True)

            # Cleanup story worker container
            await _cleanup_story_worker(redis_client, story_id)

            # Notify user — permanent failure, no auto-recovery possible
            story = await api_client.get_story(story_id) if story_id else {}
            user_id = story.get("user_id", "")
            if user_id:
                event = POSystemEvent(
                    event="story_failed",
                    text="Story permanently failed after several retry attempts.",
                    user_id=str(user_id),
                )
                await redis_client.publish_flat(PO_INPUT_QUEUE, to_flat_fields(event))

            failed += 1

    return {"retried": retried, "failed": failed}


async def supervise_stuck_tasks(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """Detect tasks stuck in in_dev and fail them.

    Failed tasks will be picked up by supervise_failed_tasks for retry.
    Returns dict with 'timed_out' count.
    """
    tasks = await api_client.get_tasks_by_status(TaskStatus.IN_DEV)
    timed_out = 0
    now = datetime.now(UTC)

    for task in tasks:
        task_id = task["id"]
        updated_at = _parse_datetime(task["updated_at"])
        age_minutes = (now - updated_at).total_seconds() / 60

        if age_minutes < TASK_STUCK_THRESHOLD_MINUTES:
            continue

        log = logger.bind(task_id=task_id, age_minutes=round(age_minutes, 1))
        log.warning("task_stuck_timeout", threshold_minutes=TASK_STUCK_THRESHOLD_MINUTES)

        await api_client.transition_task(task_id, TaskStatus.FAILED, "supervisor")
        timed_out += 1

    return {"timed_out": timed_out}

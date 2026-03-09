"""Task Dispatcher — dispatches todo tasks, completes stories, supervises pipeline.

Responsibilities:
A) Find todo tasks with no blocker (or blocker done), create Run,
   publish to engineering:queue, transition task to in_dev.
B) Find stories where all tasks are done → complete story + trigger deploy.
C) Supervise pipeline: detect stuck states, retry or fail-fast.

Runs as a periodic scheduler job (every 30s).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import structlog

from shared.contracts.queues.architect import ArchitectMessage
from shared.contracts.queues.deploy import DeployMessage, DeployTrigger
from shared.contracts.queues.engineering import EngineeringMessage
from shared.contracts.queues.po import POProactiveMessage, to_flat_fields
from shared.contracts.queues.worker import DeleteWorkerCommand
from shared.queues import ARCHITECT_QUEUE, DEPLOY_QUEUE, ENGINEERING_QUEUE, PO_PROACTIVE_QUEUE
from shared.redis_client import RedisStreamClient

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient

logger = structlog.get_logger(__name__)

DISPATCH_INTERVAL_SECONDS = 30

# Supervisor thresholds
STORY_STUCK_THRESHOLD_MINUTES = 5
TASK_STUCK_THRESHOLD_MINUTES = 30
STORY_MAX_ARCHITECT_RETRIES = 3

# Story worker registry key (shared with langgraph service)
STORY_WORKERS_KEY = "story:workers"
WORKER_COMMANDS_STREAM = "worker:commands"


async def _cleanup_story_worker(
    redis_client: RedisStreamClient,
    story_id: str,
) -> None:
    """Clean up the worker container associated with a story.

    Reads worker_id from Redis registry, sends DeleteWorkerCommand,
    then clears the registry entry.
    """
    redis = redis_client.redis
    worker_id = await redis.hget(STORY_WORKERS_KEY, story_id)
    if not worker_id:
        return

    if isinstance(worker_id, bytes):
        worker_id = worker_id.decode()

    # Send delete command to worker-manager
    delete_cmd = DeleteWorkerCommand(
        request_id=f"cleanup-story-{story_id}",
        worker_id=worker_id,
        reason="completed",
    )
    await redis.xadd(WORKER_COMMANDS_STREAM, {"data": delete_cmd.model_dump_json()})

    # Clear registry entry
    await redis.hdel(STORY_WORKERS_KEY, story_id)

    logger.info("story_worker_cleaned_up", story_id=story_id, worker_id=worker_id)


def _build_cumulative_context(sibling_events: list[dict]) -> str:
    """Build a context summary from completed sibling task events."""
    lines = []
    for event in sibling_events:
        if event.get("event_type") != "iteration_end":
            continue
        details = event.get("details", {})
        summary = details.get("summary", "")
        commit = details.get("commit_sha", "")
        if summary:
            entry = f"- {summary}"
            if commit:
                entry += f" (commit: {commit})"
            lines.append(entry)
    if not lines:
        return ""
    return "## Context from completed tasks\n" + "\n".join(lines) + "\n\n"


async def dispatch_todo_tasks(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> int:
    """Find and dispatch unblocked todo tasks.

    Returns the number of tasks dispatched.
    """
    tasks = await api_client.get_tasks_by_status("todo")
    dispatched = 0

    for task in tasks:
        task_id = task["id"]
        blocker_id = task.get("blocked_by_task_id")

        # Check if blocker is resolved
        if blocker_id:
            blocker = await api_client.get_task(blocker_id)
            if blocker.get("status") != "done":
                continue  # Still blocked

        story_id = task.get("story_id")
        project_id = task.get("project_id")
        log = logger.bind(task_id=task_id, story_id=story_id)

        # Build cumulative context from sibling tasks
        context = ""
        if story_id:
            # Get events from ALL sibling tasks (same story)
            siblings = await api_client.get_tasks_by_story(story_id)
            all_events = []
            for sibling in siblings:
                if sibling["id"] != task_id and sibling.get("status") == "done":
                    events = await api_client.get_task_events(sibling["id"])
                    all_events.extend(events)
            context = _build_cumulative_context(all_events)

        # Resolve user_id from story
        user_id = ""
        if story_id:
            story = await api_client.get_story(story_id)
            user_id = story.get("user_id", "")

        # Enrich description with context
        description = task.get("description", "")
        if context:
            description = context + description

        # Create Run
        run_id = f"eng-{uuid.uuid4().hex[:12]}"
        run_data = {
            "id": run_id,
            "type": "engineering",
            "project_id": project_id,
            "run_metadata": {
                "triggered_by": "dispatcher",
                "story_id": story_id,
                "task_id": task_id,
            },
        }
        await api_client.create_run(run_data)

        # Publish EngineeringMessage
        eng_msg = EngineeringMessage(
            task_id=run_id,
            project_id=project_id,
            user_id=str(user_id),
            action=task.get("type", "feature"),
            description=description,
            skip_deploy=True,  # Deploy handled at story level
            planning_task_id=task_id,
            story_id=story_id,
        )
        await redis_client.publish_message(ENGINEERING_QUEUE, eng_msg)

        # Transition task to in_dev
        await api_client.transition_task(task_id, "in_dev", "dispatcher")

        log.info("task_dispatched", run_id=run_id)
        dispatched += 1

    return dispatched


async def complete_stories(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> int:
    """Find stories where all tasks are done and complete them.

    Returns the number of stories completed.
    """
    stories = await api_client.get_stories_by_status("in_progress")
    completed = 0

    for story in stories:
        story_id = story["id"]
        project_id = story.get("project_id")
        user_id = story.get("user_id", "")

        tasks = await api_client.get_tasks_by_story(story_id)

        # Skip if no tasks (architect may not have run yet)
        if not tasks:
            continue

        # Check if all tasks are done
        if not all(t.get("status") == "done" for t in tasks):
            continue

        log = logger.bind(story_id=story_id, project_id=project_id)

        # Complete story
        await api_client.transition_story(story_id, "complete")
        log.info("story_completed", task_count=len(tasks))

        # Trigger deploy
        deploy_id = f"deploy-{uuid.uuid4().hex[:12]}"
        deploy_msg = DeployMessage(
            task_id=deploy_id,
            project_id=project_id,
            user_id=str(user_id),
            triggered_by=DeployTrigger.ENGINEERING,
        )
        await redis_client.publish_message(DEPLOY_QUEUE, deploy_msg)
        log.info("deploy_triggered", deploy_id=deploy_id)

        # Notify PO
        proactive = POProactiveMessage(
            text=f"Story completed: all {len(tasks)} tasks done. Deploy triggered.",
            user_id=str(user_id),
        )
        await redis_client.publish_flat(PO_PROACTIVE_QUEUE, to_flat_fields(proactive))

        # Cleanup story worker container (no longer needed)
        await _cleanup_story_worker(redis_client, story_id)

        # Trigger next queued story for this project
        await _trigger_next_story(api_client, redis_client, project_id)

        completed += 1

    return completed


async def _trigger_next_story(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    project_id: str,
) -> None:
    """Find the next created story for a project and publish to architect:queue."""
    created_stories = await api_client.get_stories_by_status("created")
    # Filter to same project, sort by priority (lower = higher priority)
    project_stories = sorted(
        [s for s in created_stories if s.get("project_id") == project_id],
        key=lambda s: s.get("priority", 0),
    )
    if not project_stories:
        return

    next_story = project_stories[0]
    arch_msg = ArchitectMessage(
        story_id=next_story["id"],
        project_id=project_id,
        user_id=next_story.get("user_id", ""),
    )
    await redis_client.publish_message(ARCHITECT_QUEUE, arch_msg)
    logger.info(
        "next_story_triggered",
        story_id=next_story["id"],
        project_id=project_id,
    )


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

    Returns dict with 'retried' and 'failed' counts.
    """
    stories = await api_client.get_stories_by_status("created")
    retried = 0
    failed = 0
    retry_counts = _retry_counts or {}

    # Build set of projects that already have an active story
    active_stories = await api_client.get_stories_by_status("in_progress")
    active_projects = {s.get("project_id") for s in active_stories}

    now = datetime.now(UTC)

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

        current_retries = retry_counts.get(story_id, 0)

        if current_retries >= STORY_MAX_ARCHITECT_RETRIES:
            log.error(
                "story_terminal_failure",
                reason="architect_retries_exhausted",
                retries=current_retries,
            )
            await api_client.fail_story(story_id)
            failed += 1
            continue

        # Retry: republish to architect:queue
        arch_msg = ArchitectMessage(
            story_id=story_id,
            project_id=story.get("project_id", ""),
            user_id=story.get("user_id", ""),
        )
        await redis_client.publish_message(ARCHITECT_QUEUE, arch_msg)
        retry_counts[story_id] = current_retries + 1

        log.warning(
            "story_stuck_retry",
            retry_attempt=retry_counts[story_id],
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
    tasks = await api_client.get_tasks_by_status("failed")
    retried = 0
    failed = 0

    for task in tasks:
        task_id = task["id"]
        story_id = task.get("story_id")

        # Skip standalone tasks (not part of a story)
        if not story_id:
            continue

        current_iter = task.get("current_iteration", 0)
        max_iter = task.get("max_iterations", 3)
        log = logger.bind(task_id=task_id, story_id=story_id, iteration=current_iter)

        if current_iter < max_iter:
            # Retry: failed → backlog → todo, bump iteration
            await api_client.transition_task(task_id, "backlog", "supervisor")
            await api_client.transition_task(task_id, "todo", "supervisor")
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
                    "done",
                    "failed",
                    "cancelled",
                ):
                    await api_client.transition_task(sibling["id"], "cancelled", "supervisor")

            await api_client.fail_story(story_id)

            # Cleanup story worker container
            await _cleanup_story_worker(redis_client, story_id)

            # Notify user
            story = await api_client.get_story(story_id) if story_id else {}
            user_id = story.get("user_id", "")
            if user_id:
                proactive = POProactiveMessage(
                    text=f"Story failed: task retries exhausted for {task_id}.",
                    user_id=str(user_id),
                )
                await redis_client.publish_flat(PO_PROACTIVE_QUEUE, to_flat_fields(proactive))

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
    tasks = await api_client.get_tasks_by_status("in_dev")
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

        await api_client.transition_task(task_id, "failed", "supervisor")
        timed_out += 1

    return {"timed_out": timed_out}


async def task_dispatcher_loop() -> None:
    """Periodic loop: dispatch tasks + complete stories every 30s."""
    from ..clients.api import api_client

    redis_client = RedisStreamClient()
    await redis_client.connect()

    logger.info("task_dispatcher_started", interval=DISPATCH_INTERVAL_SECONDS)

    story_retry_counts: dict[str, int] = {}

    try:
        while True:
            try:
                dispatched = await dispatch_todo_tasks(api_client, redis_client)
                completed = await complete_stories(api_client, redis_client)

                # Supervisor checks
                stuck_stories = await supervise_stuck_stories(
                    api_client, redis_client, _retry_counts=story_retry_counts
                )
                stuck_tasks = await supervise_stuck_tasks(api_client, redis_client)
                failed_tasks = await supervise_failed_tasks(api_client, redis_client)

                if dispatched or completed:
                    logger.info(
                        "dispatcher_cycle",
                        tasks_dispatched=dispatched,
                        stories_completed=completed,
                    )
                supervisor_active = (
                    stuck_stories["retried"]
                    + stuck_stories["failed"]
                    + stuck_tasks["timed_out"]
                    + failed_tasks["retried"]
                    + failed_tasks["failed"]
                )
                if supervisor_active:
                    logger.info(
                        "supervisor_cycle",
                        stories_retried=stuck_stories["retried"],
                        stories_failed=stuck_stories["failed"],
                        tasks_timed_out=stuck_tasks["timed_out"],
                        tasks_retried=failed_tasks["retried"],
                        tasks_failed=failed_tasks["failed"],
                    )
            except Exception:
                logger.exception("dispatcher_cycle_error")
            await asyncio.sleep(DISPATCH_INTERVAL_SECONDS)
    finally:
        await redis_client.close()
        logger.info("task_dispatcher_stopped")

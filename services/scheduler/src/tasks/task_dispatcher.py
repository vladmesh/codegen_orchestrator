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

from shared.contracts.dto.project import ProjectStatus, ServiceStatus
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.architect import ArchitectMessage
from shared.contracts.queues.deploy import DeployMessage, DeployTrigger
from shared.contracts.queues.engineering import EngineeringMessage
from shared.contracts.queues.po import POSystemEvent, to_flat_fields
from shared.contracts.queues.worker import DeleteWorkerCommand
from shared.queues import (
    ARCHITECT_QUEUE,
    DEPLOY_QUEUE,
    ENGINEERING_QUEUE,
    PO_INPUT_QUEUE,
    WORKER_COMMANDS,
)
from shared.redis_client import RedisStreamClient

from .scaffold_trigger import trigger_scaffolds

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

# Failure reasons that should not be retried by the supervisor
NON_RETRYABLE_REASONS = {"worker_rejected", "ci_infra_failure"}


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
    await redis_client.publish(WORKER_COMMANDS, delete_cmd.model_dump(mode="json"))

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
    tasks = await api_client.get_tasks_by_status(TaskStatus.TODO)
    dispatched = 0

    for task in tasks:
        task_id = task["id"]
        blocker_id = task.get("blocked_by_task_id")

        # Check if blocker is resolved
        if blocker_id:
            blocker = await api_client.get_task(blocker_id)
            if blocker.get("status") != TaskStatus.DONE:
                continue  # Still blocked

        story_id = task.get("story_id")
        project_id = task.get("project_id")
        log = logger.bind(task_id=task_id, story_id=story_id)

        # Skip internal project tasks — implemented manually via /implement
        # TODO: replace with proper project.internal flag when going to prod
        INTERNAL_PROJECT_ID = "033c2033-fc75-4d86-ade2-08efe7b15a5e"
        if project_id == INTERNAL_PROJECT_ID:
            continue

        # Guard: don't dispatch until scaffold is complete (project must be active)
        if project_id:
            project = await api_client.get_project(project_id)
            if project and project.status == ProjectStatus.DRAFT:
                log.info("task_skipped_not_scaffolded", project_status=project.status)
                continue

        # Fetch siblings once — used for both guard and context
        siblings = []
        if story_id:
            siblings = await api_client.get_tasks_by_story(story_id)

            # Guard: max 1 in_dev task per story
            if any(s.get("status") == TaskStatus.IN_DEV for s in siblings):
                log.info("task_skipped_story_busy")
                continue

            # Guard: don't dispatch if any sibling has a non-retryable failure
            if any(
                (s.get("failure_metadata") or {}).get("failure_reason") in NON_RETRYABLE_REASONS
                for s in siblings
            ):
                log.info("task_skipped_story_has_rejected_sibling")
                continue

        # Build cumulative context from sibling tasks
        context = ""
        if siblings:
            all_events = []
            for sibling in siblings:
                if sibling["id"] != task_id and sibling.get("status") == TaskStatus.DONE:
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
        await api_client.transition_task(task_id, TaskStatus.IN_DEV, "dispatcher")

        log.info("task_dispatched", run_id=run_id)
        dispatched += 1

    return dispatched


async def complete_stories(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> int:
    """Find stories where all tasks are done, transition to deploying, trigger deploy.

    Returns the number of stories transitioned.
    """
    stories = await api_client.get_stories_by_status(StoryStatus.IN_PROGRESS)
    completed = 0

    if stories:
        logger.info(
            "complete_stories_check",
            in_progress_stories=len(stories),
        )

    for story in stories:
        story_id = story["id"]
        project_id = story.get("project_id")

        # Fetch project once — used for user resolution and deploy action
        project = await api_client.get_project(project_id) if project_id else None

        # Story doesn't have user_id — resolve telegram_id from project.owner_id
        user_id = story.get("user_id", "")
        if not user_id and project:
            owner_id = getattr(project, "owner_id", None)
            if owner_id:
                user = await api_client.get_user(int(owner_id))
                if user:
                    user_id = str(user.get("telegram_id", ""))

        tasks = await api_client.get_tasks_by_story(story_id)

        # Skip if no tasks (architect may not have run yet)
        if not tasks:
            logger.debug("complete_stories_skip_no_tasks", story_id=story_id)
            continue

        task_statuses = [t.get("status") for t in tasks]
        # Check if all tasks are done
        if not all(s == TaskStatus.DONE for s in task_statuses):
            logger.debug(
                "complete_stories_skip_not_all_done",
                story_id=story_id,
                task_statuses=task_statuses,
            )
            continue

        log = logger.bind(story_id=story_id, project_id=project_id)

        # Transition story to deploying (not completed — deploy must succeed first)
        await api_client.transition_story(story_id, "deploy")
        log.info("story_deploying", task_count=len(tasks))

        # Determine deploy action based on service_status
        deploy_action = "create"
        if project and getattr(project, "service_status", None) != ServiceStatus.NOT_DEPLOYED:
            deploy_action = "feature"

        # Trigger deploy — create run record first (deploy consumer expects it)
        deploy_id = f"deploy-{uuid.uuid4().hex[:12]}"
        await api_client.create_run(
            {
                "id": deploy_id,
                "type": "deploy",
                "project_id": project_id,
                "status": "queued",
            }
        )
        deploy_msg = DeployMessage(
            task_id=deploy_id,
            project_id=project_id,
            user_id=str(user_id),
            story_id=story_id,
            triggered_by=DeployTrigger.ENGINEERING,
            action=deploy_action,
        )
        await redis_client.publish_message(DEPLOY_QUEUE, deploy_msg)
        log.info("deploy_triggered", deploy_id=deploy_id, action=deploy_action)

        # No proactive message — "all tasks done, deploy triggered" is internal.
        # User will be notified by deploy worker on success or by supervisor on
        # permanent failure.

        # Cleanup story worker container (no longer needed)
        await _cleanup_story_worker(redis_client, story_id)

        # Trigger next queued story for this project (doesn't need deploy to finish)
        await _trigger_next_story(api_client, redis_client, project_id)

        completed += 1

    return completed


async def _trigger_next_story(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    project_id: str,
) -> None:
    """Find the next created story for a project and publish to architect:queue."""
    created_stories = await api_client.get_stories_by_status(StoryStatus.CREATED)
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
    stories = await api_client.get_stories_by_status(StoryStatus.CREATED)
    retried = 0
    failed = 0
    retry_counts = _retry_counts or {}

    # Build set of projects that already have an active story
    active_stories = await api_client.get_stories_by_status(StoryStatus.IN_PROGRESS)
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
                scaffolds = await trigger_scaffolds(api_client, redis_client)
                dispatched = await dispatch_todo_tasks(api_client, redis_client)
                completed = await complete_stories(api_client, redis_client)

                # Supervisor checks
                stuck_stories = await supervise_stuck_stories(
                    api_client, redis_client, _retry_counts=story_retry_counts
                )
                stuck_tasks = await supervise_stuck_tasks(api_client, redis_client)
                failed_tasks = await supervise_failed_tasks(api_client, redis_client)

                # Always log the cycle summary for observability
                logger.info(
                    "dispatcher_cycle",
                    tasks_dispatched=dispatched,
                    stories_completed=completed,
                    scaffolds_triggered=scaffolds,
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

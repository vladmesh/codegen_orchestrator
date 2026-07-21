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
from typing import TYPE_CHECKING
import uuid

import structlog

from shared.contracts.dto.engineering import EngineeringStatus
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import EngineeringRunResult
from shared.contracts.dto.task import TaskDTO, TaskStatus, TaskType
from shared.contracts.queues.engineering import EngineeringMessage
from shared.contracts.vocab import ActionType
from shared.queues import ENGINEERING_QUEUE
from shared.redis_client import RedisStreamClient

from .pr_poller import poll_ci_failures, poll_merged_prs
from .scaffold_trigger import trigger_scaffolds
from .story_completion import (
    _cleanup_story_worker,
    _parse_owner_repo,
    _trigger_next_story,
    complete_stories,
)
from .supervisor import (
    STORY_RETRY_KEY_PREFIX,
    _parse_datetime,
    supervise_deploying_stories,
    supervise_failed_tasks,
    supervise_stuck_stories,
    supervise_stuck_tasks,
    supervise_testing_stories,
    supervise_waiting_user_secret_stories,
)

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient

# Re-export for backward compatibility with tests
__all__ = [
    "STORY_RETRY_KEY_PREFIX",
    "_build_cumulative_context",
    "_cleanup_story_worker",
    "_parse_datetime",
    "_parse_owner_repo",
    "_trigger_next_story",
    "complete_stories",
    "dispatch_todo_tasks",
    "poll_merged_prs",
    "supervise_deploying_stories",
    "supervise_failed_tasks",
    "supervise_stuck_stories",
    "supervise_stuck_tasks",
    "supervise_testing_stories",
    "supervise_waiting_user_secret_stories",
    "task_dispatcher_loop",
]

from .. import startup

logger = structlog.get_logger(__name__)

# Statuses of a run that is still owned by the engineering pipeline: the worker
# either has not picked it up yet or is working on it.
_LIVE_RUN_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING)

# error_message written on a run whose EngineeringMessage never reached the queue.
PUBLISH_FAILED_ERROR = "dispatch publish failed"


def _dispatch_interval() -> int:
    return startup.get_config().get_int("scheduler.dispatch_interval_seconds")


def _build_cumulative_context(sibling_events: list) -> str:
    """Build a context summary from completed sibling task events."""
    lines = []
    for event in sibling_events:
        if event.event_type != "iteration_end":
            continue
        details = event.details or {}
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


async def _find_live_engineering_run(api_client: SchedulerAPIClient, task_id: str) -> str | None:
    """Return the id of a live engineering run of the task, if there is one."""
    for run_status in _LIVE_RUN_STATUSES:
        runs = await api_client.list_runs(
            task_id=task_id,
            run_type=RunType.ENGINEERING.value,
            status=run_status.value,
        )
        for run in runs:
            return run.id
    return None


async def _create_and_publish_run(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    task: TaskDTO,
    description: str,
    log: structlog.BoundLogger,
) -> str | None:
    """Create the engineering run and publish its message.

    Returns the run id. If publishing fails the run is closed as FAILED — nothing
    would ever pick it up — and None is returned so the task stays in todo and the
    next tick dispatches it with a fresh run.
    """
    task_id = task.id
    story_id = task.story_id
    project_id = str(task.project_id)

    run_id = f"eng-{uuid.uuid4().hex[:12]}"
    await api_client.create_run(
        {
            "id": run_id,
            "type": RunType.ENGINEERING.value,
            "project_id": project_id,
            "task_id": task_id,
            "run_metadata": {
                "triggered_by": "dispatcher",
                "story_id": story_id,
                "task_id": task_id,
            },
        }
    )

    action = ActionType.FEATURE if task.type is TaskType.REFACTOR else ActionType(task.type)
    eng_msg = EngineeringMessage(
        task_id=run_id,
        project_id=project_id,
        user_id="",  # StoryDTO has no user_id field
        action=action,
        description=description,
        skip_deploy=True,  # Deploy handled at story level
        planning_task_id=task_id,
        story_id=story_id,
        branch=f"story/{story_id}" if story_id else None,
    )
    try:
        await redis_client.publish_message(ENGINEERING_QUEUE, eng_msg)
    except Exception:
        log.exception("task_dispatch_publish_failed", run_id=run_id)
        await api_client.update_run(
            run_id,
            {
                "status": RunStatus.FAILED.value,
                "error_message": PUBLISH_FAILED_ERROR,
                "result": EngineeringRunResult(
                    engineering_status=EngineeringStatus.FAILED
                ).model_dump(mode="json"),
            },
        )
        return None
    return run_id


async def _transition_to_in_dev(
    api_client: SchedulerAPIClient,
    task_id: str,
    run_id: str,
    log: structlog.BoundLogger,
) -> bool:
    """Move a task to in_dev, retrying once.

    The message is already out, so the run is live and the task must not stay in
    todo. If both attempts fail, the pre-dispatch guard finishes the transition on
    the next tick.
    """
    try:
        await api_client.transition_task(task_id, TaskStatus.IN_DEV, "dispatcher")
    except Exception:
        log.warning("task_transition_retry", run_id=run_id, exc_info=True)
        try:
            await api_client.transition_task(task_id, TaskStatus.IN_DEV, "dispatcher")
        except Exception:
            log.exception("task_transition_failed", run_id=run_id)
            return False
    return True


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
        task_id = task.id
        blocker_id = task.blocked_by_task_id

        # Check if blocker is resolved
        if blocker_id:
            blocker = await api_client.get_task(blocker_id)
            if blocker.status != TaskStatus.DONE:
                continue  # Still blocked

        story_id = task.story_id
        project_id = str(task.project_id)
        log = logger.bind(task_id=task_id, story_id=story_id)

        # Skip internal project tasks — implemented manually via /implement
        # TODO: replace with proper project.internal flag when going to prod
        INTERNAL_PROJECT_ID = "033c2033-fc75-4d86-ade2-08efe7b15a5e"
        if project_id == INTERNAL_PROJECT_ID:
            continue

        # Guard: don't dispatch until scaffold is complete and workspace is ready
        if project_id:
            project = await api_client.get_project(project_id)
            if project and project.status == ProjectStatus.DRAFT:
                log.info("task_skipped_not_scaffolded", project_status=project.status)
                continue
            if project and not (project.config or {}).get("workspace_ready"):
                log.info("task_skipped_workspace_not_ready", project_id=project_id)
                continue

        # Fetch siblings once — used for both guard and context
        siblings = []
        if story_id:
            siblings = await api_client.get_tasks_by_story(story_id)

            # Guard: max 1 in_dev task per story
            if any(s.status == TaskStatus.IN_DEV for s in siblings):
                log.info("task_skipped_story_busy")
                continue

            # Guard: don't dispatch if any sibling is waiting for human review
            if any(s.status == TaskStatus.WAITING_HUMAN_REVIEW for s in siblings):
                log.info("task_skipped_story_has_gave_up_sibling")
                continue

        # A live engineering run means a previous tick published the message but
        # died before the transition. Finish that transition instead of creating
        # a second run and dispatching the same task twice.
        live_run_id = await _find_live_engineering_run(api_client, task_id)
        if live_run_id:
            await api_client.transition_task(task_id, TaskStatus.IN_DEV, "dispatcher")
            log.info("task_transition_recovered", run_id=live_run_id)
            dispatched += 1
            continue

        # Build cumulative context from sibling tasks
        context = ""
        if siblings:
            all_events = []
            for sibling in siblings:
                if sibling.id != task_id and sibling.status == TaskStatus.DONE:
                    events = await api_client.get_task_events(sibling.id)
                    all_events.extend(events)
            context = _build_cumulative_context(all_events)

        # Enrich description with context
        description = task.description or ""
        if context:
            description = context + description

        run_id = await _create_and_publish_run(api_client, redis_client, task, description, log)
        if run_id is None:
            continue

        if not await _transition_to_in_dev(api_client, task_id, run_id, log):
            continue

        log.info("task_dispatched", run_id=run_id)
        dispatched += 1

    return dispatched


async def task_dispatcher_loop() -> None:
    """Periodic loop: dispatch tasks + complete stories every 30s."""
    from ..clients.api import api_client

    redis_client = RedisStreamClient()
    await redis_client.connect()

    logger.info("task_dispatcher_started", interval=_dispatch_interval())

    try:
        while True:
            try:
                scaffolds = await trigger_scaffolds(api_client, redis_client)
                dispatched = await dispatch_todo_tasks(api_client, redis_client)
                completed = await complete_stories(api_client, redis_client)
                merged = await poll_merged_prs(api_client, redis_client)
                await poll_ci_failures(api_client)

                # Supervisor checks
                stuck_stories = await supervise_stuck_stories(api_client, redis_client)
                stuck_tasks = await supervise_stuck_tasks(api_client, redis_client)
                failed_tasks = await supervise_failed_tasks(api_client, redis_client)
                deploying = await supervise_deploying_stories(api_client, redis_client)
                waiting_secret = await supervise_waiting_user_secret_stories(
                    api_client, redis_client
                )
                testing = await supervise_testing_stories(api_client, redis_client)

                # Always log the cycle summary for observability
                logger.info(
                    "dispatcher_cycle",
                    tasks_dispatched=dispatched,
                    stories_completed=completed,
                    scaffolds_triggered=scaffolds,
                    prs_merged=merged,
                )
                supervisor_active = (
                    stuck_stories.get("retried", 0)
                    + stuck_stories.get("failed", 0)
                    + stuck_tasks.get("timed_out", 0)
                    + failed_tasks.get("retried", 0)
                    + failed_tasks.get("escalated", 0)
                    + deploying.get("tested", 0)
                    + deploying.get("retried", 0)
                    + deploying.get("redispatched", 0)
                    + deploying.get("waiting", 0)
                    + deploying.get("failed", 0)
                    + waiting_secret.get("redispatched", 0)
                    + waiting_secret.get("failed", 0)
                    + testing.get("completed", 0)
                    + testing.get("redispatched", 0)
                    + testing.get("failed", 0)
                )
                if supervisor_active:
                    logger.info(
                        "supervisor_cycle",
                        stories_retried=stuck_stories.get("retried", 0),
                        stories_failed=stuck_stories.get("failed", 0),
                        tasks_timed_out=stuck_tasks.get("timed_out", 0),
                        tasks_retried=failed_tasks.get("retried", 0),
                        tasks_escalated=failed_tasks.get("escalated", 0),
                        deploy_tested=deploying.get("tested", 0),
                        deploy_retried=deploying.get("retried", 0),
                        deploy_redispatched=deploying.get("redispatched", 0),
                        deploy_waiting_user_secret=deploying.get("waiting", 0),
                        deploy_failed=deploying.get("failed", 0),
                        user_secret_redispatched=waiting_secret.get("redispatched", 0),
                        user_secret_failed=waiting_secret.get("failed", 0),
                        qa_completed=testing.get("completed", 0),
                        qa_redispatched=testing.get("redispatched", 0),
                        qa_failed=testing.get("failed", 0),
                    )
            except Exception:
                logger.exception("dispatcher_cycle_error")
            await asyncio.sleep(_dispatch_interval())
    finally:
        await redis_client.close()
        logger.info("task_dispatcher_stopped")

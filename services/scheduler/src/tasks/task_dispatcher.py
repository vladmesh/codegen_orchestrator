"""Task Dispatcher — dispatches todo tasks and completes stories.

Two responsibilities:
A) Find todo tasks with no blocker (or blocker done), create Run,
   publish to engineering:queue, transition task to in_dev.
B) Find stories where all tasks are done → complete story + trigger deploy.

Runs as a periodic scheduler job (every 30s).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
import uuid

import structlog

from shared.contracts.queues.deploy import DeployMessage, DeployTrigger
from shared.contracts.queues.engineering import EngineeringMessage
from shared.contracts.queues.po import POProactiveMessage, to_flat_fields
from shared.queues import DEPLOY_QUEUE, ENGINEERING_QUEUE, PO_PROACTIVE_QUEUE
from shared.redis_client import RedisStreamClient

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient

logger = structlog.get_logger(__name__)

DISPATCH_INTERVAL_SECONDS = 30


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

        completed += 1

    return completed


async def task_dispatcher_loop() -> None:
    """Periodic loop: dispatch tasks + complete stories every 30s."""
    from ..clients.api import api_client

    redis_client = RedisStreamClient()
    await redis_client.connect()

    logger.info("task_dispatcher_started", interval=DISPATCH_INTERVAL_SECONDS)

    try:
        while True:
            try:
                dispatched = await dispatch_todo_tasks(api_client, redis_client)
                completed = await complete_stories(api_client, redis_client)
                if dispatched or completed:
                    logger.info(
                        "dispatcher_cycle",
                        tasks_dispatched=dispatched,
                        stories_completed=completed,
                    )
            except Exception:
                logger.exception("dispatcher_cycle_error")
            await asyncio.sleep(DISPATCH_INTERVAL_SECONDS)
    finally:
        await redis_client.close()
        logger.info("task_dispatcher_stopped")

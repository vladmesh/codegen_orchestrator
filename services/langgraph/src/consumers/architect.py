"""Architect consumer — consumes from architect:queue and decomposes stories into tasks.

Run standalone: python -m src.consumers.architect
"""

from __future__ import annotations

import asyncio
import uuid

from pydantic import ValidationError
import structlog

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.architect import ArchitectMessage
from shared.queues import ARCHITECT_GROUP, ARCHITECT_QUEUE
from shared.redis_client import RedisStreamClient

from ..agents.architect.graph import create_architect_graph
from ..agents.architect.tools import reset_task_chain
from ..clients.api import api_client
from ..config.settings import get_settings
from ..tracing import build_langfuse_metadata, get_langfuse_callbacks
from ._base import start_worker

logger = structlog.get_logger(__name__)

SCAFFOLD_WAIT_INTERVAL = 10  # seconds between checks
SCAFFOLD_WAIT_MAX = 300  # max wait time (5 min)

CI_CHECK_TITLE = "Run tests, verify CI green"
CI_CHECK_DESCRIPTION = (
    "Run full test suite. Push to GitHub. Wait for CI. If CI fails, read logs, fix, and push again."
)


async def append_ci_check_task(story_id: str, project_id: str) -> dict | None:
    """Append a CI check task after architect-created tasks.

    Finds the tail of the task dependency chain and creates a CI check task
    blocked by it. Skips if no architect tasks exist for the story.

    Returns:
        Created CI task dict, or None if skipped.
    """
    tasks = await api_client.get_tasks_by_story(story_id)
    architect_tasks = [t for t in tasks if t.get("created_by") == "architect"]
    if not architect_tasks:
        return None

    # Find chain tail: a task that no other task is blocked by
    blocker_ids = {t["blocked_by_task_id"] for t in architect_tasks if t.get("blocked_by_task_id")}
    tail_tasks = [t for t in architect_tasks if t["id"] not in blocker_ids]
    last_task_id = tail_tasks[-1]["id"] if tail_tasks else architect_tasks[-1]["id"]

    ci_task = await api_client.create_task(
        {
            "title": CI_CHECK_TITLE,
            "description": CI_CHECK_DESCRIPTION,
            "type": "feature",
            "status": TaskStatus.TODO,
            "acceptance_criteria": "All tests pass. CI is green. Code is pushed to GitHub.",
            "story_id": story_id,
            "project_id": project_id,
            "blocked_by_task_id": last_task_id,
            "created_by": "system",
        }
    )
    logger.info("architect_ci_task_appended", task_id=ci_task.get("id"), blocked_by=last_task_id)
    return ci_task


async def _wait_for_scaffold(project_id: str, project: dict, log) -> tuple[dict | None, str | None]:
    """Wait for scaffold to complete (DRAFT → ACTIVE).

    Returns (project, error). If error is set, caller should abort.
    """
    if project.get("status") != ProjectStatus.DRAFT:
        return project, None

    log.info("architect_waiting_for_scaffold")
    waited = 0
    while waited < SCAFFOLD_WAIT_MAX:
        await asyncio.sleep(SCAFFOLD_WAIT_INTERVAL)
        waited += SCAFFOLD_WAIT_INTERVAL
        project = await api_client.get_project(project_id)
        if not project:
            log.warning("architect_project_deleted_during_scaffold_wait")
            return None, "project deleted during scaffold wait"
        if project.get("status") != ProjectStatus.DRAFT:
            break
        log.debug("architect_scaffold_poll", waited=waited)

    if project.get("status") == ProjectStatus.DRAFT:
        log.error("architect_scaffold_timeout", waited=waited)
        return project, "scaffold did not complete in time"

    log.info("architect_scaffold_ready", waited=waited)
    return project, None


async def process_architect_job(job_data: dict, redis: RedisStreamClient) -> dict:
    """Process a single architect job by running the Architect ReAct agent.

    Args:
        job_data: Job data from Redis queue (story_id, project_id, user_id).
        redis: Redis client (unused but required by base worker signature).

    Returns:
        Result dict with status and details.
    """
    try:
        msg = ArchitectMessage.model_validate(job_data)
    except ValidationError:
        logger.warning("architect_invalid_message", data=job_data)
        return {"status": "skipped", "error": "invalid message"}

    log = logger.bind(story_id=msg.story_id, project_id=msg.project_id)
    log.info("architect_job_started")

    # Guard: skip stories that are already done or no longer exist
    try:
        story = await api_client.get_story(msg.story_id)
    except Exception:
        log.warning("architect_story_not_found", story_id=msg.story_id)
        return {"status": "skipped", "error": "story not found"}

    story_status = story.get("status")
    skip_statuses = {
        StoryStatus.COMPLETED,
        StoryStatus.ARCHIVED,
        StoryStatus.FAILED,
        StoryStatus.DEPLOYING,
    }
    if story_status in skip_statuses:
        log.info("architect_skipping_finished_story", status=story_status)
        return {"status": "skipped", "reason": f"story already {story_status}"}

    # Skip if already in_progress with tasks (duplicate message from supervisor retry)
    # But never skip reopened stories — they need re-decomposition
    if story_status == StoryStatus.IN_PROGRESS:
        existing_tasks = await api_client.get_tasks_by_story(msg.story_id)
        if existing_tasks:
            log.info("architect_skipping_already_decomposed", task_count=len(existing_tasks))
            return {"status": "skipped", "reason": "already decomposed"}

    # Transition to in_progress immediately to prevent supervisor retries
    if story_status == StoryStatus.CREATED:
        try:
            await api_client.transition_story(msg.story_id, "start")
            log.info("architect_story_started")
        except Exception as e:
            log.warning("architect_story_start_failed", error=str(e))

    # Guard: skip if project no longer exists
    project = await api_client.get_project(msg.project_id)
    if not project:
        log.warning("architect_project_not_found", project_id=msg.project_id)
        return {"status": "skipped", "error": "project not found"}

    # Wait for scaffold completion (DRAFT → ACTIVE) before decomposing
    project, scaffold_err = await _wait_for_scaffold(msg.project_id, project, log)
    if scaffold_err:
        return {"status": "failed" if project else "skipped", "error": scaffold_err}

    settings = get_settings()

    if not settings.architect_llm_api_key:
        log.error("architect_llm_not_configured")
        return {"status": "failed", "error": "ARCHITECT_LLM_API_KEY not set"}

    try:
        reset_task_chain()
        graph = create_architect_graph(
            model=settings.architect_llm_model or "anthropic/claude-sonnet-4",
            base_url=settings.architect_llm_base_url or "https://openrouter.ai/api/v1",
            api_key=settings.architect_llm_api_key,
        )

        if msg.is_reopen:
            user_content = (
                f"This is a REOPEN of story {msg.story_id} for project {msg.project_id}. "
                f"User report: {msg.user_report}\n\n"
                f"IMPORTANT: Call get_tasks_by_story FIRST to review what was already tried. "
                f"Then call get_story and get_project_spec. "
                f"Create tasks that address the user's specific complaint, "
                f"not repeat the same approach."
            )
        else:
            user_content = (
                f"Decompose story {msg.story_id} for project {msg.project_id}. "
                f"Start by calling get_story and get_project_spec."
            )

        initial_state = {
            "messages": [{"role": "user", "content": user_content}],
            "story_id": msg.story_id,
            "project_id": msg.project_id,
            "user_id": msg.user_id,
        }

        config = {
            "configurable": {"thread_id": str(uuid.uuid4())},
            "callbacks": get_langfuse_callbacks(),
            "metadata": build_langfuse_metadata(
                agent_type="architect",
                user_id=msg.user_id,
                project_id=msg.project_id,
                story_id=msg.story_id,
            ),
        }
        result = await graph.ainvoke(initial_state, config=config)

        await append_ci_check_task(msg.story_id, msg.project_id)

        # Transition reopened stories to in_progress so dispatcher can pick up tasks
        if story_status == StoryStatus.REOPENED:
            try:
                await api_client.transition_story(msg.story_id, "start")
                log.info("architect_reopened_story_started")
            except Exception as e:
                log.warning("architect_reopened_story_start_failed", error=str(e))

        log.info(
            "architect_job_success",
            message_count=len(result.get("messages", [])),
        )
        return {"status": "success"}

    except Exception as e:
        log.error(
            "architect_job_failed",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        return {"status": "failed", "error": str(e)}


def main():
    """Entry point for running as module."""
    start_worker(
        service_name="architect",
        queue=ARCHITECT_QUEUE,
        process_fn=process_architect_job,
        group=ARCHITECT_GROUP,
    )


if __name__ == "__main__":
    main()

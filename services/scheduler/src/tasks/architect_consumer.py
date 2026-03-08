"""Architect consumer — decomposes stories into tasks via LLM.

Consumes architect:queue, calls LLM for structured decomposition,
creates tasks via API with dependency chains.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, ValidationError
import structlog

from shared.contracts.queues.architect import ArchitectMessage
from shared.queues import ARCHITECT_GROUP, ARCHITECT_QUEUE
from shared.redis_client import RedisStreamClient

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient

logger = structlog.get_logger(__name__)

CONSUMER_NAME = f"architect-{os.getpid()}"


# ---------------------------------------------------------------------------
# LLM structured output schema
# ---------------------------------------------------------------------------
class TaskDecomposition(BaseModel):
    """A single task produced by architect decomposition."""

    title: str
    description: str
    type: str = "feature"
    acceptance_criteria: str | None = None
    blocked_by_index: int | None = None  # index into the task list (0-based)


DECOMPOSE_SYSTEM_PROMPT = """\
You are an architect that decomposes user stories into implementation tasks.

Given a story description, project context, and existing tasks, produce a list of \
concrete implementation tasks. Each task should be small enough for a single \
engineering run (1-3 files changed).

Rules:
- Order tasks by dependency: foundational work first (models, schemas), then API, then UI.
- Use blocked_by_index to express dependencies (0-based index into your task list).
- A task can only be blocked by ONE earlier task (the most critical dependency).
- Keep tasks focused — each should have a clear, testable outcome.
- Set type to one of: "create", "feature", "fix", "refactor".
- Include acceptance_criteria for each task.

Respond with a JSON array of task objects. Each object has:
- title (string)
- description (string)
- type (string: create/feature/fix/refactor)
- acceptance_criteria (string)
- blocked_by_index (integer or null — index of the blocking task in this list)
"""


async def call_llm_decompose(
    *,
    story: dict,
    project: dict,
    existing_tasks: list[dict],
) -> list[dict]:
    """Call LLM to decompose a story into tasks.

    Uses OpenRouter-compatible API via httpx.
    Returns a list of task dicts with fields matching TaskDecomposition.
    """
    api_key = os.getenv("ARCHITECT_LLM_API_KEY")
    if not api_key:
        raise RuntimeError("ARCHITECT_LLM_API_KEY is not set")

    base_url = os.getenv("ARCHITECT_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    model = os.getenv("ARCHITECT_LLM_MODEL", "anthropic/claude-sonnet-4")

    project_config = project.get("config") or {}
    detailed_spec = project_config.get("detailed_spec", "No spec available")

    existing_tasks_summary = ""
    if existing_tasks:
        lines = [f"- {t.get('title', 'untitled')} ({t.get('status', '?')})" for t in existing_tasks]
        existing_tasks_summary = "\n\nExisting tasks for this story:\n" + "\n".join(lines)

    user_prompt = (
        "## Story\n"
        f"**Title**: {story.get('title', '')}\n"
        f"**Description**: {story.get('description', '')}\n\n"
        f"## Project: {project.get('name', 'unknown')}\n"
        f"**Spec**: {detailed_spec}\n"
        f"{existing_tasks_summary}\n\n"
        "Decompose this story into implementation tasks. Return a JSON array."
    )

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": DECOMPOSE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.3,
                "response_format": {"type": "json_object"},
            },
        )
        resp.raise_for_status()

    content = resp.json()["choices"][0]["message"]["content"]
    parsed = json.loads(content)

    # Handle both {"tasks": [...]} and [...] formats
    if isinstance(parsed, dict):
        parsed = parsed.get("tasks", [])

    # Validate each task
    result = []
    for item in parsed:
        try:
            task = TaskDecomposition.model_validate(item)
            result.append(task.model_dump())
        except ValidationError:
            logger.warning("architect_invalid_task_skipped", item=item)

    return result


async def decompose_story(
    msg: ArchitectMessage,
    api_client: SchedulerAPIClient,
) -> None:
    """Decompose a story into tasks via LLM and create them via API.

    Args:
        msg: The architect queue message with story_id, project_id, user_id.
        api_client: API client for fetching context and creating tasks.
    """
    log = logger.bind(story_id=msg.story_id, project_id=msg.project_id)

    # Fetch context
    story = await api_client.get_story(msg.story_id)
    project = await api_client.get_project(msg.project_id)
    existing_tasks = await api_client.get_tasks_by_story(msg.story_id)

    log.info(
        "architect_decomposing",
        story_title=story.get("title", ""),
        existing_task_count=len(existing_tasks),
    )

    # Call LLM
    task_defs = await call_llm_decompose(
        story=story,
        project=project,
        existing_tasks=existing_tasks,
    )

    if not task_defs:
        log.warning("architect_no_tasks_generated")
        return

    # Create tasks with dependency chains
    created_task_ids: list[str] = []
    for task_def in task_defs:
        blocked_by_index = task_def.get("blocked_by_index")
        blocked_by_task_id = None
        if blocked_by_index is not None and 0 <= blocked_by_index < len(created_task_ids):
            blocked_by_task_id = created_task_ids[blocked_by_index]

        task_data = {
            "project_id": msg.project_id,
            "story_id": msg.story_id,
            "title": task_def["title"],
            "description": task_def.get("description", ""),
            "type": task_def.get("type", "feature"),
            "acceptance_criteria": task_def.get("acceptance_criteria"),
            "status": "todo",
            "blocked_by_task_id": blocked_by_task_id,
            "created_by": "architect",
        }

        created = await api_client.create_task(task_data)
        created_task_ids.append(created["id"])
        log.info(
            "architect_task_created",
            task_id=created["id"],
            title=task_def["title"],
            blocked_by=blocked_by_task_id,
        )

    # Transition story to in_progress
    await api_client.transition_story(msg.story_id, "start")
    log.info("architect_story_started", task_count=len(created_task_ids))


async def _process_architect_message(
    data: dict[str, Any],
    api_client: SchedulerAPIClient,
) -> None:
    """Validate and process a single architect queue message."""
    try:
        msg = ArchitectMessage.model_validate(data)
    except ValidationError:
        logger.warning("architect_invalid_message", data=data)
        return

    await decompose_story(msg, api_client)


async def architect_consumer_loop() -> None:
    """Consumer loop for architect:queue.

    Reads ArchitectMessage entries, processes them concurrently
    with a semaphore to limit parallelism.
    """
    from ..clients.api import api_client

    redis_client = RedisStreamClient()
    await redis_client.connect()

    log = logger.bind(stream=ARCHITECT_QUEUE, consumer=CONSUMER_NAME)
    log.info("architect_consumer_started")

    sem = asyncio.Semaphore(5)

    async def _handle(msg_data: dict, msg_id: str) -> None:
        async with sem:
            try:
                await _process_architect_message(msg_data, api_client)
            except Exception:
                log.exception("architect_processing_error", msg_id=msg_id)

    try:
        async for msg in redis_client.consume(
            ARCHITECT_QUEUE,
            ARCHITECT_GROUP,
            CONSUMER_NAME,
            auto_ack=False,
            claim_pending=True,
        ):
            if msg is None:
                continue
            task = asyncio.create_task(_handle(msg.data, msg.message_id))
            # Fire-and-forget but ack after completion
            task.add_done_callback(
                lambda _, mid=msg.message_id: asyncio.create_task(
                    redis_client.ack(ARCHITECT_QUEUE, ARCHITECT_GROUP, mid)
                )
            )
    finally:
        await redis_client.close()
        log.info("architect_consumer_stopped")

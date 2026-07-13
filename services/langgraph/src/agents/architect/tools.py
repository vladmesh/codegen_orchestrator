"""Architect agent tools.

Tools for the architect ReAct agent to decompose stories into tasks.
All tools use the shared LanggraphAPIClient singleton.

Task chaining: create_task auto-chains tasks sequentially — each new task
is blocked by the previous one. The LLM doesn't need to track task IDs
or manage dependencies.
"""

from __future__ import annotations

from http import HTTPStatus

import httpx
from langchain_core.tools import tool
import structlog

from shared.contracts.dto.task import TaskStatus

from ...clients.api import api_client

logger = structlog.get_logger(__name__)

# Auto-chaining state: tracks the last created task ID per story.
# Reset between architect invocations (module is long-lived but each graph
# invocation starts fresh via reset_task_chain).
_last_task_id: dict[str, str] = {}


def reset_task_chain() -> None:
    """Reset auto-chaining state. Call before each architect invocation."""
    _last_task_id.clear()


@tool
async def get_story(story_id: str) -> dict:
    """Get a story by ID. Returns title, description, status, and project_id."""
    story = await api_client.get_story(story_id)
    return story.model_dump(mode="json")


@tool
async def get_project_spec(project_id: str, detail: str = "") -> dict:
    """Get project overview, file tree, and spec summaries.

    By default returns a compact overview: project metadata, file tree,
    module list, and specs_summary (model names, domains, events).
    This is usually enough for task decomposition.

    Use `detail` only when the summary is insufficient for a specific decision:
        detail="models"  — full model definitions with fields and types
        detail="events"  — full event definitions
        detail="domains" — full domain operations with methods and paths

    Args:
        project_id: Project ID.
        detail: Optional detail level. Empty for summary, or one of:
            "models", "events", "domains".
    """
    project = await api_client.get_project(project_id)
    if project is None:
        return {"error": f"Project {project_id} not found"}

    result = project.model_dump(mode="json")
    config = project.config or {}
    specs_summary = config.get("specs_summary", {})

    # Always include tree and basic info
    result["tree"] = config.get("tree")

    # Strip noisy fields from the config in the result dict
    result_config = result.get("config") or {}
    for key in ("secrets", "env_hints", "specs_summary"):
        result_config.pop(key, None)

    if not detail:
        # Compact summary: just names and counts
        compact = {}
        if specs_summary.get("models"):
            compact["models"] = [m["name"] for m in specs_summary["models"]]
        if specs_summary.get("events"):
            compact["events"] = [e["name"] for e in specs_summary["events"]]
        if specs_summary.get("domains"):
            compact["domains"] = [
                f"{d['service']}/{d['domain']} ({len(d['operations'])} ops)"
                for d in specs_summary["domains"]
            ]
        result["specs"] = compact
    elif detail == "models":
        result["specs_detail"] = {"models": specs_summary.get("models", [])}
    elif detail == "events":
        result["specs_detail"] = {"events": specs_summary.get("events", [])}
    elif detail == "domains":
        result["specs_detail"] = {"domains": specs_summary.get("domains", [])}
    else:
        result["specs"] = {"error": f"Unknown detail: {detail}. Use: models, events, domains"}

    return result


@tool
async def get_tasks_by_story(story_id: str) -> list[dict]:
    """Get all existing tasks for a story. Use to check what work already exists."""
    tasks = await api_client.get_tasks_by_story(story_id)
    return [t.model_dump(mode="json") for t in tasks]


@tool
async def create_task(
    title: str,
    description: str,
    type: str,
    acceptance_criteria: str,
    story_id: str,
    project_id: str,
) -> dict:
    """Create a new task for a story.

    Tasks are automatically chained: each new task is blocked by the previous
    one created for the same story. Just call create_task in the right order —
    dependencies are handled for you.

    Args:
        title: Short task title.
        description: What needs to be done.
        type: One of: create, feature, fix, refactor.
        acceptance_criteria: How to verify the task is done.
        story_id: Parent story ID.
        project_id: Parent project ID.
    """
    blocked_by = _last_task_id.get(story_id)

    task_data = {
        "title": title,
        "description": description,
        "type": type,
        "acceptance_criteria": acceptance_criteria,
        "story_id": story_id,
        "project_id": project_id,
        "status": TaskStatus.TODO,
        "blocked_by_task_id": blocked_by,
        "created_by": "architect",
    }
    result = await api_client.create_task(task_data)

    # Track for auto-chaining
    task_id = result.id
    if task_id:
        _last_task_id[story_id] = task_id

    logger.info(
        "architect_task_created",
        task_id=task_id,
        title=title,
        blocked_by=blocked_by,
    )
    return result.model_dump(mode="json")


@tool
async def update_acceptance_criteria(project_id: str, acceptance_criteria: str) -> dict:
    """Update the repository's acceptance criteria for regression testing.

    Call this AFTER creating all tasks. Pass the COMPLETE updated list of
    acceptance criteria — not just the new ones. Read the current criteria
    first (returned in the response), add checks for new functionality from
    this story, and remove checks for deleted functionality.

    Format: one check per line, starting with "- ". Each check should be
    concrete and verifiable via curl or Telegram command:
        - GET /health returns 200
        - POST /api/cities with {"name": "Moscow"} returns 201
        - Telegram: /start responds with welcome message

    Args:
        project_id: Project ID (same as used in create_task).
        acceptance_criteria: The FULL updated acceptance criteria text.
    """
    repo = await api_client.get_primary_repository(project_id)
    if not repo:
        return {"error": f"No repository found for project {project_id}"}

    updated = await api_client.update_repository(
        repo.id, {"acceptance_criteria": acceptance_criteria}
    )
    logger.info(
        "architect_acceptance_criteria_updated",
        repo_id=repo.id,
        criteria_length=len(acceptance_criteria),
    )
    return {
        "repo_id": updated.id,
        "acceptance_criteria": updated.acceptance_criteria,
    }


@tool
async def transition_story(story_id: str, action: str) -> dict:
    """Transition a story's status.

    Args:
        story_id: The story ID.
        action: One of: start, complete, archive.
    """
    try:
        story = await api_client.transition_story(story_id, action)
        return story.model_dump(mode="json")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY:
            # Story already in target state (e.g. PO already started it)
            story = await api_client.get_story(story_id)
            return story.model_dump(mode="json")
        raise


def get_architect_tools() -> list:
    """Return all architect tools."""
    return [
        get_story,
        get_project_spec,
        get_tasks_by_story,
        create_task,
        update_acceptance_criteria,
        transition_story,
    ]

"""Architect agent tools.

Tools for the architect ReAct agent to decompose stories into tasks.
All tools use the shared LanggraphAPIClient singleton.
"""

from __future__ import annotations

from langchain_core.tools import tool
import structlog

from ...clients.api import api_client

logger = structlog.get_logger(__name__)


@tool
async def get_story(story_id: str) -> dict:
    """Get a story by ID. Returns title, description, status, and project_id."""
    return await api_client.get_story(story_id)


@tool
async def get_project_spec(project_id: str) -> dict:
    """Get project details including tree, specs, and config.

    Returns project with tree (from scaffolder) surfaced at top level.
    Noisy config fields (secrets, env_hints) are stripped to save tokens.
    """
    result = await api_client.get_project(project_id)
    if result is None:
        return {"error": f"Project {project_id} not found"}

    config = result.get("config") or {}
    result["tree"] = config.get("tree")
    for key in ("secrets", "env_hints"):
        config.pop(key, None)

    return result


@tool
async def get_tasks_by_story(story_id: str) -> list[dict]:
    """Get all existing tasks for a story. Use to check what work already exists."""
    return await api_client.get_tasks_by_story(story_id)


@tool
async def create_task(
    title: str,
    description: str,
    type: str,
    acceptance_criteria: str,
    story_id: str,
    project_id: str,
    blocked_by_task_id: str | None = None,
) -> dict:
    """Create a new task for a story.

    Args:
        title: Short task title.
        description: What needs to be done.
        type: One of: create, feature, fix, refactor.
        acceptance_criteria: How to verify the task is done.
        story_id: Parent story ID.
        project_id: Parent project ID.
        blocked_by_task_id: ID of task that must complete first (use IDs from
            previously created tasks in this session).
    """
    task_data = {
        "title": title,
        "description": description,
        "type": type,
        "acceptance_criteria": acceptance_criteria,
        "story_id": story_id,
        "project_id": project_id,
        "status": "todo",
        "blocked_by_task_id": blocked_by_task_id,
        "created_by": "architect",
    }
    result = await api_client.create_task(task_data)
    logger.info(
        "architect_task_created",
        task_id=result.get("id"),
        title=title,
        blocked_by=blocked_by_task_id,
    )
    return result


@tool
async def transition_story(story_id: str, action: str) -> dict:
    """Transition a story's status.

    Args:
        story_id: The story ID.
        action: One of: start, complete, archive.
    """
    return await api_client.transition_story(story_id, action)


def get_architect_tools() -> list:
    """Return all architect tools."""
    return [get_story, get_project_spec, get_tasks_by_story, create_task, transition_story]

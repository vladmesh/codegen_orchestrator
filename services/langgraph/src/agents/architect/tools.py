"""Architect agent tools.

Tools for the architect ReAct agent to decompose stories into tasks.
All tools use the shared LanggraphAPIClient singleton.
"""

from __future__ import annotations

from langchain_core.tools import tool
import structlog

from shared.contracts.dto.task import TaskStatus

from ...clients.api import api_client

logger = structlog.get_logger(__name__)


@tool
async def get_story(story_id: str) -> dict:
    """Get a story by ID. Returns title, description, status, and project_id."""
    return await api_client.get_story(story_id)


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
    result = await api_client.get_project(project_id)
    if result is None:
        return {"error": f"Project {project_id} not found"}

    config = result.get("config") or {}
    specs_summary = config.get("specs_summary", {})

    # Always include tree and basic info
    result["tree"] = config.get("tree")

    # Strip noisy fields
    for key in ("secrets", "env_hints", "specs_summary"):
        config.pop(key, None)

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
        "status": TaskStatus.TODO,
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

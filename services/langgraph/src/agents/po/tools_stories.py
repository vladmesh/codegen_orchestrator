"""PO tools — story and run management (create, list, reopen, get story/run)."""

from __future__ import annotations

import json

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
import structlog

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.story import StoryType
from shared.contracts.queues.architect import ArchitectMessage
from shared.queues import ARCHITECT_QUEUE

from .tools_shared import _get_api, _get_stream_client, _user_headers

logger = structlog.get_logger(__name__)


@tool
async def create_story(
    project_id: str,
    title: str,
    description: str,
    story_type: str = "feature",
    *,
    config: RunnableConfig,
) -> str:
    """Create a user story for a project and send it to the architect for decomposition.

    This is the main way to request work on a project — whether creating it
    from scratch, adding features, or fixing bugs. The architect will decompose
    the story into tasks and start engineering work automatically.

    IMPORTANT: The description should contain the full gathered requirements —
    not just the user's original short message. Compose a detailed spec from
    the clarifying conversation before calling this tool.

    Args:
        project_id: Project ID.
        title: Short title for the story (e.g. "Currency rate alerts",
            "Fix login button", "Create telegram bot for recipes").
        description: Detailed description of what to build or fix.
            Include all requirements gathered from the conversation.
        story_type: "feature" (new functionality or project creation),
            "fix" (bug fix).
    """
    api = _get_api()
    headers = _user_headers(config)

    # Determine action from project status, not story_type
    if story_type == "fix":
        action = "fix"
    else:
        proj_resp = await api.get(f"/api/projects/{project_id}", headers=headers)
        proj_resp.raise_for_status()
        project_status = proj_resp.json().get("status", ProjectStatus.DRAFT)
        action = "create" if project_status == ProjectStatus.DRAFT else "feature"

    # 1. Create story via API (API generates the ID)
    story_payload = {
        "project_id": project_id,
        "title": title,
        "description": description,
        "type": StoryType.PRODUCT.value,
        "created_by": "po",
    }
    resp = await api.post("/api/stories/", json=story_payload, headers=headers)
    resp.raise_for_status()
    story_id = resp.json()["id"]
    logger.info("po_story_created", story_id=story_id, project_id=project_id, title=title)

    # 2. Check if project already has an active story (sequential processing)
    user_id = config["configurable"].get("user_id", "unknown")
    active_stories_resp = await api.get(
        f"/api/stories/?project_id={project_id}&status=in_progress", headers=headers
    )
    active_stories = active_stories_resp.json() if active_stories_resp.is_success else []

    if active_stories:
        # Queue the story — it will be triggered when current story completes
        logger.info(
            "po_story_queued",
            story_id=story_id,
            project_id=project_id,
            active_story=active_stories[0]["id"],
        )
        return (
            f"Story created and queued (ID: {story_id}). "
            f"Another story is in progress — this one will start automatically when it completes."
        )

    # No active story — publish to architect:queue for decomposition
    arch_msg = ArchitectMessage(
        story_id=story_id,
        project_id=project_id,
        user_id=user_id,
    )
    await _get_stream_client().publish_message(ARCHITECT_QUEUE, arch_msg)

    # 3. Persist description to project config for action=create
    if action == "create" and description:
        try:
            proj_resp = await api.get(f"/api/projects/{project_id}", headers=headers)
            proj_resp.raise_for_status()
            current_config = proj_resp.json().get("config", {})
            current_config["detailed_spec"] = description
            patch_resp = await api.patch(
                f"/api/projects/{project_id}",
                json={"config": current_config},
                headers=headers,
            )
            patch_resp.raise_for_status()
        except Exception:
            logger.warning(
                "failed_to_persist_detailed_spec",
                project_id=project_id,
                exc_info=True,
            )

    logger.info("po_story_submitted_to_architect", story_id=story_id, action=action)
    return (
        f"Story created and sent to architect for decomposition.\n"
        f"Story: {story_id} — {title}\n"
        f"The architect will break it into tasks and start engineering work."
    )


@tool
async def list_stories(project_id: str, *, config: RunnableConfig) -> str:
    """List all stories for a project.

    Args:
        project_id: Project ID.
    """
    api = _get_api()
    headers = _user_headers(config)
    resp = await api.get(f"/api/stories/?project_id={project_id}", headers=headers)
    resp.raise_for_status()
    stories = resp.json()

    if not stories:
        return "No stories found for this project."

    lines = []
    for s in stories:
        lines.append(f"- [{s['status']}] {s['title']} (ID: {s['id']}, type: {s.get('type', '?')})")
    return "\n".join(lines)


@tool
async def reopen_story(story_id: str, user_report: str, *, config: RunnableConfig) -> str:
    """Reopen a completed story instead of creating a new one.

    Use this when the user reports a problem with something that was already
    built in a previous story. The user_report carries their feedback through
    the entire pipeline (PO → Architect → Developer).

    Args:
        story_id: ID of the completed story to reopen.
        user_report: User's description of what's wrong (e.g. "images work
            sometimes but not always", "layout is broken on mobile").
    """
    api = _get_api()
    headers = _user_headers(config)
    user_id = config["configurable"].get("user_id", "unknown")

    resp = await api.post(
        f"/api/stories/{story_id}/reopen",
        json={"user_report": user_report, "actor": "po"},
        headers=headers,
    )
    resp.raise_for_status()
    story = resp.json()

    arch_msg = ArchitectMessage(
        story_id=story_id,
        project_id=story["project_id"],
        user_id=user_id,
        is_reopen=True,
        user_report=user_report,
    )
    await _get_stream_client().publish_message(ARCHITECT_QUEUE, arch_msg)

    logger.info(
        "po_story_reopened",
        story_id=story_id,
        project_id=story["project_id"],
    )
    return (
        f"Story reopened and sent to architect for re-decomposition.\n"
        f"Story: {story_id} — {story['title']}\n"
        f"User report: {user_report}\n"
        f"The architect will review previous tasks and create new ones."
    )


@tool
async def get_story(story_id: str, *, config: RunnableConfig) -> str:
    """Get story details including linked tasks, their statuses, and runs.

    Args:
        story_id: Story ID (e.g. story-abc12345).
    """
    api = _get_api()
    headers = _user_headers(config)

    # Get story
    resp = await api.get(f"/api/stories/{story_id}", headers=headers)
    resp.raise_for_status()
    story = resp.json()

    # Get tasks linked to this story
    tasks_resp = await api.get(f"/api/tasks/?story_id={story_id}", headers=headers)
    tasks_resp.raise_for_status()
    tasks = tasks_resp.json()

    # Fetch runs for each task
    enriched_tasks = []
    for t in tasks:
        task_info = {"id": t["id"], "status": t["status"], "type": t["type"]}
        runs_resp = await api.get(f"/api/runs/?task_id={t['id']}", headers=headers)
        if runs_resp.is_success:
            runs = runs_resp.json()
            task_info["runs"] = [
                {
                    "id": r["id"],
                    "status": r["status"],
                    "type": r["type"],
                    "error_message": r.get("error_message"),
                    "started_at": r.get("started_at"),
                    "completed_at": r.get("completed_at"),
                }
                for r in runs
            ]
        enriched_tasks.append(task_info)

    result = {
        "story": story,
        "tasks": enriched_tasks,
    }
    return json.dumps(result, indent=2, ensure_ascii=False)


@tool
async def get_run_status(run_id: str, *, config: RunnableConfig) -> str:
    """Get status of an engineering or deploy run.

    Args:
        run_id: Run ID (e.g. eng-abc123 or deploy-abc123).
    """
    api = _get_api()
    headers = _user_headers(config)
    resp = await api.get(f"/api/runs/{run_id}", headers=headers)
    resp.raise_for_status()
    run = resp.json()
    return json.dumps(run, indent=2, ensure_ascii=False)

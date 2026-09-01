"""PO tools — story and run management (create, list, reopen, get story/run)."""

from __future__ import annotations

import json
import uuid

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
import structlog

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.story import StoryType
from shared.contracts.queues.architect import ArchitectMessage
from shared.queues import ARCHITECT_QUEUE

from .tools_shared import _get_api, _get_stream_client, _user_headers

logger = structlog.get_logger(__name__)


def _qa_failure_hold(stories: list[dict], parent_story_id: str | None) -> tuple[str, dict] | None:
    """Return an unresolved QA failure only when retrying that story chain."""
    if parent_story_id is None:
        return None

    for story in stories:
        if story.get("status") != "waiting_human_review":
            continue
        if story.get("id") != parent_story_id:
            continue
        reason = story.get("quarantine_reason") or {}
        evidence = reason.get("qa_failure")
        if isinstance(evidence, dict):
            return story["id"], evidence
    return None


@tool
async def prepare_product_brief(
    project_id: str,
    title: str,
    intended_users: list[str],
    languages: list[str],
    must_requirements: list[dict],
    initial_settings: list[dict],
    *,
    config: RunnableConfig,
) -> str:
    """Create one unconfirmed, structured summary before product-story creation.

    Call once after gathering requirements. Send the returned summary verbatim
    and wait for the user's yes or correction. Corrections require a new call.
    """
    payload = {
        "project_id": project_id,
        "title": title,
        # A correction is a new draft revision. Confirmation, keyed below by
        # brief identity, remains restart-idempotent for the exact presented
        # content without reusing a stale draft request identity.
        "request_id": f"po-brief-{config['configurable']['thread_id']}-{uuid.uuid4().hex}",
        "content": {
            "intended_users": intended_users,
            "languages": languages,
            "must_requirements": must_requirements,
            "initial_settings": initial_settings,
        },
    }
    response = await _get_api().post_raw(
        "product-briefs/", json=payload, headers=_user_headers(config)
    )
    response.raise_for_status()
    brief = response.json()
    content = brief["content"]
    requirements = "\n".join(
        f"- [{item['id']}] {item['text']}" for item in content["must_requirements"]
    )
    return (
        f"Product Brief {brief['id']} (revision {brief['revision']})\n"
        f"Intended users: {', '.join(content['intended_users'])}\n"
        f"Languages: {', '.join(content['languages'])}\n"
        f"Must-requirements:\n{requirements}\n"
        f"Initial settings: {json.dumps(content['initial_settings'], ensure_ascii=False)}\n\n"
        "yes / correct me"
    )


@tool
async def confirm_product_brief(brief_id: str, content: dict, *, config: RunnableConfig) -> str:
    """Record the user's explicit yes for the exact presented Product Brief."""
    response = await _get_api().post_raw(
        f"product-briefs/{brief_id}/confirm",
        json={
            "request_id": f"po-confirm-{config['configurable']['thread_id']}-{brief_id}",
            "content": content,
        },
        headers=_user_headers(config),
    )
    response.raise_for_status()
    config["configurable"]["confirmed_product_brief_id"] = brief_id
    return f"Product Brief {brief_id} confirmed. You may now create the product story."


@tool
async def create_story(
    project_id: str,
    title: str,
    description: str,
    story_type: str = "feature",
    parent_story_id: str | None = None,
    product_brief_id: str | None = None,
    *,
    config: RunnableConfig,
) -> str:
    """Create a user story for a project and send it to the architect for decomposition.

    This is the main way to request work on a project — whether creating it
    from scratch, adding features, or fixing bugs. The architect will decompose
    the story into tasks and start engineering work automatically.

    Product work requires a previously confirmed Product Brief. Do not compose
    requirements from prose or call this before its one user confirmation.

    Args:
        project_id: Project ID.
        title: Short title for the story (e.g. "Currency rate alerts",
            "Fix login button", "Create telegram bot for recipes").
        description: Detailed description of what to build or fix.
            Include all requirements gathered from the conversation.
        story_type: "feature" (new functionality or project creation),
            "fix" (bug fix).
        parent_story_id: Story this work retries or continues, if any.
    """
    api = _get_api()
    headers = _user_headers(config)

    product_brief_id = product_brief_id or config["configurable"].get("confirmed_product_brief_id")
    is_fix = story_type == "fix"
    if not is_fix and not product_brief_id:
        return "No story was created: product work requires a confirmed Product Brief."

    # Determine action from project status, not story_type
    if is_fix:
        action = "fix"
    else:
        proj_resp = await api.get_raw(f"projects/{project_id}", headers=headers)
        proj_resp.raise_for_status()
        project_status = proj_resp.json().get("status", ProjectStatus.DRAFT)
        action = "create" if project_status == ProjectStatus.DRAFT else "feature"

    stories_resp = await api.get_raw(f"stories/?project_id={project_id}", headers=headers)
    stories_resp.raise_for_status()
    project_stories = stories_resp.json()
    reminder_story_id = config["configurable"].get("retry_story_id", "")
    if reminder_story_id and not any(
        story.get("id") == reminder_story_id for story in project_stories
    ):
        logger.warning(
            "po_retry_provenance_not_found",
            project_id=project_id,
            reminder_story_id=reminder_story_id,
        )
        return (
            "No story was created because the reminder's source story could not be verified. "
            "Please ask a human to review the retry."
        )
    retry_parent_story_id = reminder_story_id or parent_story_id
    if reminder_story_id and parent_story_id and parent_story_id != reminder_story_id:
        logger.warning(
            "po_retry_parent_overridden",
            requested_parent_story_id=parent_story_id,
            reminder_story_id=reminder_story_id,
        )
    if hold := _qa_failure_hold(project_stories, retry_parent_story_id):
        held_story_id, evidence = hold
        fingerprint = evidence.get("fingerprint", "unknown")
        logger.warning(
            "po_story_blocked_by_qa_failure",
            project_id=project_id,
            held_story_id=held_story_id,
            failure_fingerprint=fingerprint,
        )
        return (
            "No story was created. A repeated QA failure is waiting for human review "
            f"on story {held_story_id} (fingerprint: {fingerprint}). "
            "Please ask a human to decide whether and how to continue."
        )

    # 1. Create story via API (API generates the ID)
    story_payload = {
        "project_id": project_id,
        "title": title,
        "description": description,
        "parent_story_id": retry_parent_story_id,
        "type": StoryType.TECHNICAL.value if is_fix else StoryType.PRODUCT.value,
        "created_by": "po",
        "product_brief_id": None if is_fix else product_brief_id,
    }
    resp = await api.post_raw("stories/", json=story_payload, headers=headers)
    resp.raise_for_status()
    story_id = resp.json()["id"]
    logger.info("po_story_created", story_id=story_id, project_id=project_id, title=title)

    # The architect needs this spec when decomposing a newly created project.
    # Persist it before any path can publish the story for downstream work.
    if action == "create" and description:
        current_config = proj_resp.json().get("config", {})
        current_config["detailed_spec"] = description
        patch_resp = await api.patch_raw(
            f"projects/{project_id}",
            json={"config": current_config},
            headers=headers,
        )
        patch_resp.raise_for_status()

    # 2. Check if project already has an active story (sequential processing)
    telegram_chat_id = config["configurable"]["telegram_chat_id"]
    active_stories = [story for story in project_stories if story.get("status") == "in_progress"]

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
        telegram_chat_id=telegram_chat_id,
    )
    await _get_stream_client().publish_message(ARCHITECT_QUEUE, arch_msg)

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
    resp = await api.get_raw(f"stories/?project_id={project_id}", headers=headers)
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
    telegram_chat_id = config["configurable"]["telegram_chat_id"]

    resp = await api.post_raw(
        f"stories/{story_id}/reopen",
        json={"user_report": user_report, "actor": "po"},
        headers=headers,
    )
    resp.raise_for_status()
    story = resp.json()

    arch_msg = ArchitectMessage(
        story_id=story_id,
        project_id=story["project_id"],
        telegram_chat_id=telegram_chat_id,
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
    resp = await api.get_raw(f"stories/{story_id}", headers=headers)
    resp.raise_for_status()
    story = resp.json()

    # Get tasks linked to this story
    tasks_resp = await api.get_raw(f"tasks/?story_id={story_id}", headers=headers)
    tasks_resp.raise_for_status()
    tasks = tasks_resp.json()

    # Fetch runs for each task
    enriched_tasks = []
    for t in tasks:
        task_info = {"id": t["id"], "status": t["status"], "type": t["type"]}
        runs_resp = await api.get_raw(f"runs/?task_id={t['id']}", headers=headers)
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
    resp = await api.get_raw(f"runs/{run_id}", headers=headers)
    resp.raise_for_status()
    run = resp.json()
    return json.dumps(run, indent=2, ensure_ascii=False)

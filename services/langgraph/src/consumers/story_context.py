"""Build story context and STORY.md for engineering workers."""

from __future__ import annotations

import structlog

from shared.contracts.dto.task import TaskStatus

from ..clients.api import api_client

logger = structlog.get_logger(__name__)


async def build_story_context(story_id: str, current_task_id: str | None = None) -> str | None:
    """Build a compact list of sibling tasks for story context.

    Skips the current task (already in TASK.md). Shows only titles and statuses —
    no descriptions, events, or reports to avoid duplication and info leakage.
    Returns None if story has no tasks or fetch fails.
    """
    try:
        tasks = await api_client.get_tasks_by_story(story_id)
    except Exception:
        logger.warning("story_context_fetch_failed", story_id=story_id, exc_info=True)
        return None

    if not tasks:
        return None

    lines: list[str] = []
    try:
        story = await api_client.get_story(story_id)
        user_report = story.user_report
        if user_report:
            lines.append("## User Report")
            lines.append(user_report)
            lines.append("")
    except Exception:
        logger.debug("story_fetch_for_user_report_failed", story_id=story_id)

    tasks.sort(key=lambda t: t.created_at or "")
    pending_statuses = {TaskStatus.BACKLOG, TaskStatus.TODO, TaskStatus.BLOCKED}
    for task in tasks:
        tid = task.id
        title = task.title
        status = task.status

        if tid == current_task_id:
            continue

        if status == "done":
            lines.append(f"- ~~{title}~~ — done (see .story/old_tasks/)")
        elif status in pending_statuses:
            lines.append(f"- {title} [{status}] — do NOT implement")
        else:
            lines.append(f"- {title} [{status}]")

    return "\n".join(lines)


async def build_story_md(story_id: str, current_task_id: str | None = None) -> str | None:
    """Build .story/STORY.md content for the worker's file-first context.

    Returns a markdown string with story goal, task list, and references.
    Unlike build_story_context (which embeds everything in the prompt),
    this creates a file the worker reads only when needed.
    """
    try:
        story = await api_client.get_story(story_id)
    except Exception:
        logger.warning("story_md_fetch_failed", story_id=story_id, exc_info=True)
        return None

    try:
        tasks = await api_client.get_tasks_by_story(story_id)
    except Exception:
        tasks = []

    title = story.title
    description = story.description or ""

    lines = [f"# Story: {title}", ""]
    if description:
        lines.append("## Goal")
        lines.append(description)
        lines.append("")

    user_report = story.user_report
    if user_report:
        lines.append("## User Report")
        lines.append(user_report)
        lines.append("")

    if tasks:
        tasks.sort(key=lambda t: t.created_at or "")
        lines.append("## Tasks")
        for i, task in enumerate(tasks, 1):
            tid = task.id
            task_title = task.title
            status = task.status
            is_current = tid == current_task_id
            if is_current:
                lines.append(f"{i}. **{task_title}** — current (see TASK.md)")
            elif status == "done":
                lines.append(f"{i}. ~~{task_title}~~ — done (see old_tasks/)")
            else:
                lines.append(f"{i}. {task_title} [{status}]")
        lines.append("")

    lines.append("## References")
    lines.append("- `README.md` — project description")
    lines.append("- `.env` / `.env.example` — environment variables")
    lines.append("- `AGENTS.md` — code patterns and conventions")
    lines.append("- `.story/old_tasks/` — completed tasks with developer reports")
    lines.append("")

    return "\n".join(lines)

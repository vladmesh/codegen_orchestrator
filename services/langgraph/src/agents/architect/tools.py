"""Architect agent tools.

Tools for the architect ReAct agent to decompose stories into tasks.
All tools use the shared LanggraphAPIClient singleton.

Task chaining: create_task auto-chains tasks sequentially — each new task
is blocked by the previous one. The LLM doesn't need to track task IDs
or manage dependencies.

Story lifecycle is deliberately absent: the architect consumer moves the story
to IN_PROGRESS around this agent's run, and an agent tool that moved it too
gave one code path two Story transitions for the same story.

Planning identity is deliberately absent from every tool schema: the brief id
and the planning attempt id arrive through `InjectedState`, so the model can
neither invent an attempt nor plan into a brief it was not given. A run that is
not planning under a brief carries `None` for both, and then `create_task`
sends the shape it always sent and `record_requirement_coverage` refuses.
"""

from __future__ import annotations

from typing import Annotated

import httpx
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from pydantic import ValidationError
import structlog

from shared.contracts.dto.product_brief import RequirementCoverageCreate
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
    planning_attempt_id: Annotated[str | None, InjectedState("planning_attempt_id")] = None,
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
    if planning_attempt_id is not None:
        # Planning under a Product Brief: the API creates the task unadmitted
        # under this attempt, and only `POST /product-briefs/{id}/admit`
        # releases it. Absent otherwise, so an ordinary task is created with the
        # shape it has always been created with.
        task_data["planning_attempt_id"] = planning_attempt_id
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
        planning_attempt_id=planning_attempt_id,
    )
    return result.model_dump(mode="json")


def _refusal_detail(error: httpx.HTTPStatusError) -> str:
    """What the API refused, in the words it refused it with.

    The architect's next move depends on which refusal this was — an unknown
    requirement id is a different repair from a disposition that named both a
    task and a reason — so the detail is handed back to the model rather than
    flattened into "failed".
    """
    try:
        body = error.response.json()
    except ValueError:
        return error.response.text or str(error)
    detail = body.get("detail") if isinstance(body, dict) else None
    return str(detail) if detail else str(body)


@tool
async def record_requirement_coverage(
    requirement_id: str,
    task_id: str = "",
    returned_reason: str = "",
    brief_id: Annotated[str | None, InjectedState("product_brief_id")] = None,
    planning_attempt_id: Annotated[str | None, InjectedState("planning_attempt_id")] = None,
) -> dict:
    """Record how you disposed of ONE must-requirement of the Product Brief.

    Exactly one disposition per requirement id, and exactly one of the two
    arguments: the id of the task that covers it, or the reason it is being
    returned undone. Neither is not an answer and both is two answers.

    Nothing in this story's plan is released until every must-requirement id has
    a disposition recorded here, so call this once per requirement — including
    the ones you are returning.

    Args:
        requirement_id: The must-requirement id, exactly as it was given to you.
        task_id: The task created for it, when a task covers it.
        returned_reason: Why it is being returned, when no task covers it.
    """
    if not brief_id or not planning_attempt_id:
        return {
            "error": (
                "this story is not planned under a Product Brief planning attempt; "
                "there is no requirement coverage to record"
            )
        }
    try:
        coverage = RequirementCoverageCreate(
            requirement_id=requirement_id,
            planning_attempt_id=planning_attempt_id,
            task_id=task_id or None,
            returned_reason=returned_reason or None,
        )
    except ValidationError as invalid:
        return {"error": f"invalid disposition for {requirement_id}: {invalid}"}
    try:
        recorded = await api_client.record_requirement_coverage(brief_id, coverage)
    except httpx.HTTPStatusError as refused:
        detail = _refusal_detail(refused)
        logger.warning(
            "architect_requirement_coverage_refused",
            brief_id=brief_id,
            requirement_id=requirement_id,
            status_code=refused.response.status_code,
            detail=detail,
        )
        return {"error": f"coverage for {requirement_id} was refused: {detail}"}
    logger.info(
        "architect_requirement_coverage_recorded",
        brief_id=brief_id,
        requirement_id=requirement_id,
        task_id=coverage.task_id,
        returned=coverage.returned_reason is not None,
    )
    return recorded.model_dump(mode="json")


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


def get_architect_tools() -> list:
    """Return all architect tools."""
    return [
        get_story,
        get_project_spec,
        get_tasks_by_story,
        create_task,
        record_requirement_coverage,
        update_acceptance_criteria,
    ]

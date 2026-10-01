"""PO tools — story and run management (create, list, reopen, get story/run)."""

from __future__ import annotations

from datetime import UTC, datetime
from http import HTTPStatus
import json

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import ValidationError
import structlog

from shared.contracts.dto.pr_conflict_repair import (
    PRConflictRepairCommand,
    PRConflictRepairOutcome,
    PRConflictRepairRead,
)
from shared.contracts.dto.product_brief import ProductBriefRead, ProductBriefStoryBind
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.story import (
    StoryType,
    StoryUnverifiedDecisionCreate,
    StoryUnverifiedDecisionKind,
)
from shared.contracts.dto.story_failure import StoryFailureCode
from shared.contracts.queues.architect import ArchitectMessage
from shared.queues import ARCHITECT_QUEUE

from .situation import ApiSituationReader, SituationSubject, build_situation
from .tools_briefs import clear_brief_pointer
from .tools_shared import _get_api, _get_stream_client, _user_headers

logger = structlog.get_logger(__name__)

#: How many times the bind is tried before the story is closed. More than one
#: because a 5xx or a dropped connection says nothing about whether the bind is
#: possible; small because every further attempt delays closing a story the
#: scheduler would otherwise pick up.
BIND_ATTEMPTS = 2


async def _confirmed_brief(
    project_id: str, brief_id: str, headers: dict[str, str]
) -> tuple[ProductBriefRead | None, str | None]:
    """The confirmed brief this story will be planned against, or why it is not.

    Read before the story is created, so that a brief which cannot back a story
    costs nothing: the refusal reaches the model instead of an unbound story
    reaching the architect.
    """
    response = await _get_api().get_raw(f"product-briefs/{brief_id}", headers=headers)
    if response.status_code == HTTPStatus.NOT_FOUND:
        return None, f"No story was created: Product Brief {brief_id} does not exist."
    response.raise_for_status()
    brief = ProductBriefRead.model_validate(response.json())
    if str(brief.project_id) != project_id:
        return None, (f"No story was created: Product Brief {brief_id} belongs to another project.")
    if brief.confirmed_at is None:
        return None, (
            f"No story was created: Product Brief {brief_id} is not confirmed yet. "
            "Show the user the presented revision and call confirm_product_brief "
            "once they answer yes."
        )
    if brief.story_id is not None:
        return None, (
            f"No story was created: Product Brief {brief_id} already backs story "
            f"{brief.story_id}. A new story needs a new brief."
        )
    return brief, None


def _detail_of(response) -> str:
    try:
        return response.json().get("detail")
    except ValueError:
        return response.text


async def _abandon_unbound_story(story_id: str, headers: dict[str, str]) -> str | None:
    """Close a story the brief could not be bound to. Or say why it is still open.

    Returning without publishing is not enough to keep an unbound story away
    from the architect. The story row stays `created` with no tasks, and that is
    exactly the shape the scheduler's liveness sweep re-publishes minutes later
    — which would plan the story from its prose `description` and bypass the
    brief the user confirmed, the one failure this whole boundary exists to
    prevent. So the PO closes what it orphaned: `created -> failed` is a
    declared transition, and a failed story is picked up by neither the stuck
    sweep nor the next-story trigger.
    """
    response = await _get_api().post_raw(
        f"stories/{story_id}/fail", json={"actor": "po"}, headers=headers
    )
    if response.is_success:
        logger.info("po_unbound_story_failed", story_id=story_id)
        return None
    detail = _detail_of(response)
    logger.error(
        "po_unbound_story_still_open",
        story_id=story_id,
        status_code=response.status_code,
        detail=detail,
    )
    return detail


async def _bind_brief_to_story(brief_id: str, story_id: str, headers: dict[str, str]) -> str | None:
    """Make the story brief-backed, or say why it is not. Nothing else may publish.

    The bind is what the architect sees: it reads the brief by story id, claims
    the planning attempt and plans under it. A story published to the architect
    without it would be planned as ordinary prose work, and the requirement the
    user confirmed would silently stop being the thing being built.

    A refusal (4xx) is an answer and is taken as one; only a server-side failure
    is tried again, because that is the shape a lost connection or a restarting
    API takes. When no attempt binds, the story is closed rather than left for
    the scheduler to plan as prose — see `_abandon_unbound_story`.
    """
    bind = ProductBriefStoryBind(story_id=story_id)
    for attempt in range(BIND_ATTEMPTS):
        response = await _get_api().post_raw(
            f"product-briefs/{brief_id}/story", json=bind.model_dump(mode="json"), headers=headers
        )
        if response.is_success:
            return None
        detail = _detail_of(response)
        logger.error(
            "po_product_brief_bind_failed",
            brief_id=brief_id,
            story_id=story_id,
            status_code=response.status_code,
            detail=detail,
            attempt=attempt + 1,
        )
        if response.status_code < HTTPStatus.INTERNAL_SERVER_ERROR:
            break

    still_open = await _abandon_unbound_story(story_id, headers)
    if still_open is not None:
        return (
            f"Story {story_id} was created but the confirmed Product Brief {brief_id} "
            f"could not be bound to it ({detail}), and the story could not be closed "
            f"either ({still_open}). It was NOT sent to the architect. Ask a human to "
            f"close story {story_id} before anything plans it from its description."
        )
    return (
        f"Story {story_id} was created but the confirmed Product Brief {brief_id} could "
        f"not be bound to it ({detail}), so it was NOT sent to the architect and was "
        f"closed as failed. Product Brief {brief_id} is still confirmed and unspent: "
        "tell the user what happened and call create_story again with the same "
        "product_brief_id."
    )


#: The answer to a `create_story` call without a brief: no story is created,
#: and the two things the call may have meant are named.
_NO_BRIEF_REFUSAL = (
    "No story was created: every story you create needs a confirmed Product Brief. "
    "For new work, call present_product_brief, send the user the message it returns, "
    "and after their yes call confirm_product_brief — then call create_story again "
    "with product_brief_id. A retry after a failure or a complaint about something "
    "already built is not a new story: find the original story with list_stories and "
    "call reopen_story on it."
)


@tool
async def create_story(
    project_id: str,
    title: str,
    description: str,
    product_brief_id: str | None = None,
    *,
    config: RunnableConfig,
) -> str:
    """Create the user story for a confirmed Product Brief and send it to the architect.

    This is how new work the user ordered starts:
    the first story of a project and every later feature alike.
    The architect will decompose the story into tasks and start engineering
    work automatically.

    Every story needs a confirmed Product Brief: present it with
    `present_product_brief`, confirm it with `confirm_product_brief` once the
    user says yes, and pass the brief id here. Without it no story is created.
    A retry after a failure and a complaint about something already built are
    not new stories: use `reopen_story` on the original story.

    IMPORTANT: The description should contain the full gathered requirements —
    not just the user's original short message. Compose a detailed spec from
    the clarifying conversation before calling this tool.

    Args:
        project_id: Project ID.
        title: Short title for the story (e.g. "Currency rate alerts",
            "Create telegram bot for recipes").
        description: Detailed description of what to build.
            Include all requirements gathered from the conversation.
        product_brief_id: The confirmed Product Brief this story is planned
            against. Required. Each story needs its own brief — the one bound
            to an earlier story is spent.
    """
    api = _get_api()
    headers = _user_headers(config)

    if not product_brief_id:
        # The prose-summary path is not a fallback for a missing brief: the
        # architect would plan against a re-derived interpretation of the
        # conversation instead of what the user confirmed, and the story would
        # be nobody's order.
        logger.warning("po_story_without_confirmed_brief", project_id=project_id)
        return _NO_BRIEF_REFUSAL

    # The action decides whether the spec is persisted for a new project.
    proj_resp = await api.get_raw(f"projects/{project_id}", headers=headers)
    proj_resp.raise_for_status()
    project_status = proj_resp.json().get("status", ProjectStatus.DRAFT)
    action = "create" if project_status == ProjectStatus.DRAFT else "feature"

    brief, refusal = await _confirmed_brief(project_id, product_brief_id, headers)
    if refusal is not None:
        return refusal

    stories_resp = await api.get_raw(f"stories/?project_id={project_id}", headers=headers)
    stories_resp.raise_for_status()
    project_stories = stories_resp.json()

    # 1. Create story via API (API generates the ID)
    story_payload = {
        "project_id": project_id,
        "title": title,
        "description": description,
        "type": StoryType.PRODUCT.value,
        "created_by": "po",
    }
    resp = await api.post_raw("stories/", json=story_payload, headers=headers)
    resp.raise_for_status()
    story_id = resp.json()["id"]
    logger.info("po_story_created", story_id=story_id, project_id=project_id, title=title)

    # Bind before anything can hand the story on. Whether it is published now or
    # queued behind an active story, what the architect eventually picks up must
    # already be brief-backed.
    if failure := await _bind_brief_to_story(brief.id, story_id, headers):
        return failure

    # The architect needs this spec when decomposing a newly created project.
    # Persist it before any path can publish the story for downstream work.
    if action == "create" and description:
        patch_resp = await api.patch_raw(
            f"projects/{project_id}/config",
            json={"values": {"detailed_spec": description}},
            headers=headers,
        )
        patch_resp.raise_for_status()

    # The presented-brief pointer is spent once the brief is bound: the brief is
    # reachable by its story from here on. Cleared after the spec write above,
    # which writes back a config read before the bind and would otherwise put
    # the pointer back.
    await clear_brief_pointer(project_id, headers)

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

    A story whose planning failed carries `PROBLEM:` on its line: it is not
    being built, whatever its status says. Report it as a problem the platform
    is handling, never as "in progress, no errors".

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
        line = f"- [{s['status']}] {s['title']} (ID: {s['id']}, type: {s.get('type', '?')})"
        if problem := _planning_problem(s):
            line += f" — PROBLEM: {problem}"
        lines.append(line)
    return "\n".join(lines)


#: The statuses a story is reopened from: a `completed` one the user complains
#: about, a `failed` one the platform retries. A parked story
#: (`waiting_human_review`) is returned by a person, never reopened from chat.
REOPENABLE_STATUSES = frozenset({"completed", "failed"})
#: Conflict repair stops refused before any Run: the repair attempt is unspent,
#: so the same repair request is retried once the cause (e.g. budget) is fixed.
CONFLICT_REPAIR_REFUSAL_STOPS = frozenset(
    {
        StoryFailureCode.ENGINEERING_BUDGET_DENIED.value,
        StoryFailureCode.ENGINEERING_DISPATCH_REFUSED.value,
    }
)


@tool
async def reopen_story(
    story_id: str, user_report: str | None = None, *, config: RunnableConfig
) -> str:
    """Reopen the original story instead of creating a new one.

    The one way to redo work on a story: the same story is planned again, so it
    stays the user's order and its outcome is told to them as that order's.

    - A user complaint about a `completed` story: `user_report` is required and
      carries their words through the pipeline (PO → Architect → Developer).
    - A platform retry of a `failed` story: `user_report` is optional; pass it
      only when the user said what went wrong.
    - A story waiting for human review because its PR was refused as dirty:
      request one bounded conflict repair on its existing branch and PR. The
      same request retries a repair the platform refused to start (for example
      no budget) once that cause is fixed.

    Args:
        story_id: ID of the completed or failed story to reopen.
        user_report: The user's description of what's wrong (e.g. "images work
            sometimes but not always"). Required for a completed story.
    """
    api = _get_api()
    headers = _user_headers(config)
    telegram_chat_id = config["configurable"]["telegram_chat_id"]

    current = await api.get_raw(f"stories/{story_id}", headers=headers)
    current.raise_for_status()
    record = current.json()
    status = record["status"]
    quarantine = record.get("quarantine_reason") or {}
    if status == "waiting_human_review" and (
        (
            quarantine.get("reason") == "github_app_merge_refused"
            and quarantine.get("mergeable_state") == "dirty"
            and quarantine.get("pr_number") == record.get("pr_number")
        )
        or quarantine.get("code") in CONFLICT_REPAIR_REFUSAL_STOPS
    ):
        command = PRConflictRepairCommand(
            project_id=record["project_id"],
            pr_number=record["pr_number"],
            cycle_started_at=record.get("reopened_at") or record["created_at"],
        )
        response = await api.post_raw(
            f"stories/{story_id}/repair-pr-conflicts",
            json=command.model_dump(mode="json"),
            headers=headers,
        )
        response.raise_for_status()
        repaired = PRConflictRepairRead.model_validate(response.json())
        if repaired.outcome is PRConflictRepairOutcome.EXHAUSTED:
            return f"Conflict repair stopped for story {story_id}: {repaired.reason}"
        return (
            f"Story {story_id} resumed for conflict repair in Task {repaired.task_id}. "
            "The existing pull request and its work are preserved; normal dispatch will run it."
        )
    if status not in REOPENABLE_STATUSES:
        return (
            f"Story {story_id} was not reopened: it is {status}. Only a completed or a "
            "failed story is reopened; a story waiting for human review is returned by a "
            "person, and a story in work is still being built."
        )
    if status == "completed" and not user_report:
        return (
            f"Story {story_id} was not reopened: it is completed, so reopening it is a "
            "complaint and needs user_report — the user's own description of what is wrong."
        )

    resp = await api.post_raw(
        f"stories/{story_id}/reopen",
        json={"user_report": user_report or None, "actor": "po"},
        headers=headers,
    )
    resp.raise_for_status()
    story = resp.json()

    arch_msg = ArchitectMessage(
        story_id=story_id,
        project_id=story["project_id"],
        telegram_chat_id=telegram_chat_id,
        is_reopen=True,
        user_report=user_report or None,
    )
    await _get_stream_client().publish_message(ARCHITECT_QUEUE, arch_msg)

    logger.info(
        "po_story_reopened",
        story_id=story_id,
        project_id=story["project_id"],
        reopened_from=status,
    )
    reported = f"User report: {user_report}\n" if user_report else ""
    return (
        f"Story reopened and sent to architect for re-decomposition.\n"
        f"Story: {story_id} — {story['title']}\n"
        f"{reported}"
        f"The architect will review previous tasks and create new ones."
    )


#: Story statuses in which work is stopped and the owner is owed the reason.
_STOPPED_STATUSES = frozenset({"failed", "waiting_human_review"})

#: How long an `in_progress` story may have no task before `get_story` says so.
#: Planning takes minutes; the supervisor stops a planless story after an hour.
PLANLESS_NOTICE_MINUTES = 15


def _minutes_since(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    moment = datetime.fromisoformat(timestamp)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (datetime.now(UTC) - moment).total_seconds() / 60


def _planning_problem(story: dict) -> str | None:
    """A failed planning of this story, in plain words, or None.

    Read off the story itself, so it is there even when diagnostics are not.
    """
    planning = story.get("planning") or {}
    failure = (planning.get("last_failure") or {}).get("detail", "no recorded cause")
    if planning.get("state") == "retrying":
        attempts = planning.get("failed_attempts")
        bound = planning.get("max_retries")
        return (
            f"planning the work failed ({attempts} of {bound} automatic retries used); "
            "a problem the platform is handling: it is retrying it automatically, and "
            f"nothing has been built yet. Last cause: {failure}"
        )
    if story.get("status") == "waiting_human_review" and planning.get("state") == "parked":
        return (
            "planning the work failed and could not be retried automatically: a problem "
            "the platform is handling, but work is stopped until an operator re-runs "
            f"planning, and nothing has been built yet. Cause: {failure}"
        )
    return None


def _problem(story: dict, diagnostics: dict | None) -> str | None:
    """What stops or threatens this story, in one line the PO must relay, or None."""
    planning = _planning_problem(story)
    if planning is not None:
        return planning
    if diagnostics is None:
        return None
    failure = diagnostics.get("failure")
    if failure:
        return f"{failure['code']}: {failure['detail']}"
    scaffold_error = diagnostics.get("scaffold_error")
    if scaffold_error:
        return f"the project repository could not be created: {scaffold_error}"
    if story.get("status") in _STOPPED_STATUSES:
        return diagnostics.get("quarantine_reason") or "stopped; no recorded cause"
    if story.get("status") == "in_progress" and not diagnostics.get("work_cycle_tasks"):
        idle = _minutes_since(story.get("updated_at"))
        if idle is not None and idle >= PLANLESS_NOTICE_MINUTES:
            return (
                f"in_progress for {int(idle)} minutes and no development task exists: "
                "work has not started"
            )
    return None


async def _read_diagnostics(
    story_id: str, headers: dict[str, str], *, include_logs: bool
) -> dict | None:
    response = await _get_api().get_raw(
        f"stories/{story_id}/diagnostics",
        headers=headers,
        params={"include_logs": str(include_logs).lower()},
    )
    if not response.is_success:
        logger.warning(
            "po_story_diagnostics_unavailable", story_id=story_id, status=response.status_code
        )
        return None
    return response.json()


@tool
async def get_story(story_id: str, *, config: RunnableConfig) -> str:
    """Get story details including linked tasks, their statuses, and runs.

    The result carries `problem` when something stops or threatens the story
    (a recorded failure, a failed planning the platform is retrying or has
    stopped, a project repository that could not be created, a stop, or
    `in_progress` with no task). When `problem` is set, work is NOT going
    normally: tell the user so, with the cause — never "in progress, no errors".
    A failed planning is a problem the platform is handling: report it as one —
    it retries on its own, or waits for an operator to re-run planning — and
    say nothing has been built yet.

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

    diagnostics = await _read_diagnostics(story_id, headers, include_logs=False)
    result = {
        "story": story,
        "tasks": enriched_tasks,
        "problem": _problem(story, diagnostics),
    }
    return json.dumps(result, indent=2, ensure_ascii=False)


@tool
async def get_product_situation(project_id: str, *, config: RunnableConfig) -> str:
    """What is true now for one of the user's projects: the situation snapshot.

    Call it when the user asks how their work is going, and when they return
    after a pause (read its deferred notices first). It covers the project's
    current or latest ordered story (order date, status and how long it has not
    changed), when the user last wrote, whether the deployed app is up, their
    other ordered stories in work and deferred notices. It sends nothing.

    Args:
        project_id: Project ID (UUID).
    """
    telegram_chat_id = config["configurable"]["telegram_chat_id"]
    reader = ApiSituationReader(_get_api())
    owned = await reader.list_owned_projects(int(telegram_chat_id))
    if project_id not in {str(project.id) for project in owned}:
        return f"No project {project_id} among this user's projects."
    return await build_situation(
        reader,
        _get_stream_client().redis,
        SituationSubject(telegram_chat_id=telegram_chat_id, project_id=project_id),
        requested=True,
    )


#: What the tool answers after each decision is recorded, so the next move is
#: the one this decision asks for and nothing is started by the answer itself.
_AFTER_DECISION = {
    StoryUnverifiedDecisionKind.ACCEPT_UNVERIFIED: (
        "The user's answer is recorded: they accept these checks as unverified. "
        "Nothing else changes."
    ),
    StoryUnverifiedDecisionKind.CHANGE_REQUIREMENT: (
        "The user's answer is recorded: they want the requirement changed. Nothing is "
        "reopened or rerun by it. Settle the change as a follow-up feature: agree the "
        "corrected requirement with the user, worded as something QA can check, confirm a "
        "corrected brief for it (present_product_brief, then confirm_product_brief) and "
        "create it as its own story with create_story."
    ),
}


@tool
async def record_unverified_decision(
    story_id: str,
    decision: StoryUnverifiedDecisionKind,
    check_names: list[str],
    *,
    config: RunnableConfig,
) -> str:
    """Record the user's answer about checks QA could not run on their story.

    Call it once the user has answered the message about a `story_completed` or
    `story_quarantined` event whose QA left checks unverified. The answer is
    added to the story's record; an earlier answer is kept, never replaced.
    Recording it reopens and reruns nothing.

    Args:
        story_id: Story ID the event named (e.g. story-abc12345).
        decision: `accept_unverified` — the user accepts the result without those
            checks; `change_requirement` — the user wants the requirement changed,
            which you follow up as a corrected brief confirmed as its own story.
        check_names: The names of the unverified checks the answer is about,
            exactly as the event listed them.
    """
    try:
        body = StoryUnverifiedDecisionCreate(
            decision=decision, check_names=check_names, recorded_by="po"
        )
    except ValidationError as invalid:
        return f"The answer was not recorded: {invalid}"
    response = await _get_api().post_raw(
        f"stories/{story_id}/unverified-decisions",
        json=body.model_dump(mode="json"),
        headers=_user_headers(config),
    )
    if not response.is_success:
        detail = _detail_of(response)
        logger.warning(
            "po_unverified_decision_refused",
            story_id=story_id,
            status=response.status_code,
            detail=detail,
        )
        return f"The answer was not recorded: {detail}"
    recorded = response.json()["unverified_decisions"][-1]
    logger.info(
        "po_unverified_decision_recorded",
        story_id=story_id,
        decision=body.decision,
        qa_run_id=recorded["qa_run_id"],
    )
    return _AFTER_DECISION[body.decision]


@tool
async def get_story_diagnostics(story_id: str, *, config: RunnableConfig) -> str:
    """Read why a story failed, is blocked or is not moving: causes and recent error logs.

    Read-only. Returns the recorded failure reason, the project's scaffold error,
    failed runs, task failures and the newest error/warning log lines about the
    story or its project (bounded and with secrets redacted). Call it whenever
    `get_story` shows `problem`, status `failed` or `waiting_human_review`, or
    the user asks what went wrong.

    Args:
        story_id: Story ID (e.g. story-abc12345).
    """
    diagnostics = await _read_diagnostics(story_id, _user_headers(config), include_logs=True)
    if diagnostics is None:
        return f"Diagnostics for {story_id} could not be read."
    return json.dumps(diagnostics, indent=2, ensure_ascii=False)


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

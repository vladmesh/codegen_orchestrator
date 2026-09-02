"""Architect consumer — consumes from architect:queue and decomposes stories into tasks.

Run standalone: python -m src.consumers.architect
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, nullcontext, suppress
from dataclasses import dataclass
import uuid

import structlog

from shared.contracts.dto.product_brief import (
    PLANNING_ATTEMPT_HEARTBEAT_TIMEOUT_SECONDS,
    MustRequirement,
    ProductBriefAdmissionOutcome,
    ProductBriefPlanningAttemptOutcome,
)
from shared.contracts.dto.project import ProjectDTO, ProjectStatus
from shared.contracts.dto.story import StoryStatus
from shared.contracts.queues.architect import ArchitectMessage
from shared.queues import ARCHITECT_GROUP, ARCHITECT_QUEUE
from shared.redis_client import RedisStreamClient

from ..agents.architect.graph import create_architect_graph
from ..agents.architect.tools import reset_task_chain
from ..clients.api import api_client
from ..config.agent_llm_env import missing_llm_env
from ..config.settings import get_settings
from ._base import start_worker, validate_queued_message
from ._live_work import live_work_settled, live_work_unsettled

logger = structlog.get_logger(__name__)

SCAFFOLD_WAIT_INTERVAL = 10  # seconds between checks
SCAFFOLD_WAIT_MAX = 300  # max wait time (5 min)

#: How often the owning architect proves it is alive. Strictly below the one
#: timeout the brief contract declares, and by a whole multiple of it, so a
#: single lost heartbeat — a slow API call, one retryable failure — does not
#: hand this architect's plan to a second one while it is still planning.
PLANNING_HEARTBEAT_INTERVAL = PLANNING_ATTEMPT_HEARTBEAT_TIMEOUT_SECONDS / 3


@dataclass(frozen=True)
class _PlanningAttempt:
    """The plan this run owns: which brief, which attempt, what it must dispose of."""

    brief_id: str
    planning_attempt_id: str
    must_requirements: list[MustRequirement]


async def _heartbeat_planning_attempt(brief_id: str, planning_attempt_id: str, log) -> None:
    """Refresh the claim until cancelled.

    A failed beat is logged and the loop continues: the brief row is the
    authority on who owns the plan, and it answers again — through the coverage
    writes and the one admission step, both of which refuse anyone but the
    active attempt. Turning a transient API error here into a failed job would
    throw away a plan the architect may still be allowed to finish.
    """
    while True:
        await asyncio.sleep(PLANNING_HEARTBEAT_INTERVAL)
        try:
            await api_client.heartbeat_planning_attempt(brief_id, planning_attempt_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("architect_planning_heartbeat_failed", error=str(e))


@asynccontextmanager
async def _planning_heartbeat(attempt: _PlanningAttempt, log):
    """Hold the claim for the body, and stop beating however the body ends.

    Success, an LLM exception and cancellation all leave through the same
    `finally`, so no beat outlives the job that owns the claim — a heartbeat
    still running after the consumer returned would keep a dead architect's
    plan alive and lock every other architect out of it for good.
    """
    beat = asyncio.create_task(
        _heartbeat_planning_attempt(attempt.brief_id, attempt.planning_attempt_id, log)
    )
    try:
        yield
    finally:
        beat.cancel()
        with suppress(asyncio.CancelledError):
            await beat


async def _release_planning_attempt(attempt: _PlanningAttempt, log) -> None:
    """Give the incomplete plan back, so recovery need not wait out the timeout.

    Called when this run will not admit — the planner failed, or the plan came
    back incomplete. Nothing is released by this: `finish` gives up ownership,
    and only `admit` ever crosses the boundary. Failing to give it up is not
    worth failing the job over — the claim goes stale on its own within
    `PLANNING_ATTEMPT_HEARTBEAT_TIMEOUT_SECONDS`, and a story left behind an
    incomplete plan needs an operator either way, because
    `supervise_stuck_stories` scans `StoryStatus.CREATED` only — so it is logged
    rather than raised over whatever went wrong first.
    """
    try:
        await api_client.finish_planning_attempt(attempt.brief_id, attempt.planning_attempt_id)
        log.info("architect_planning_attempt_released", brief_id=attempt.brief_id)
    except Exception as e:
        log.warning("architect_planning_attempt_release_failed", error=str(e))


async def _claim_planning_attempt(
    story_id: str, log
) -> tuple[_PlanningAttempt | None, dict | None]:
    """Decide what this run plans under, before the graph is invoked.

    Returns `(attempt, early_result)`. `(None, None)` is the ordinary run: the
    story has no brief, or its brief was already admitted, and either way this
    run creates no task under an attempt and admits nothing. An `early_result`
    means another architect owns the plan and this run must not touch it.
    """
    brief = await api_client.get_product_brief_by_story(story_id)
    if brief is None:
        return None, None
    if brief.confirmed_at is None:
        # Nothing is planned against a brief the user has not confirmed: its
        # content is still open to revision, and the API would refuse the claim.
        log.info("architect_product_brief_unconfirmed", brief_id=brief.id)
        return None, live_work_settled(
            {
                "status": "skipped",
                "reason": "Product Brief is not confirmed yet",
                "brief_id": brief.id,
            }
        )

    claim = await api_client.claim_planning_attempt(brief.id)
    log = log.bind(brief_id=brief.id)
    match claim.outcome:
        case ProductBriefPlanningAttemptOutcome.CLAIMED:
            if claim.planning_attempt_id is None:
                raise RuntimeError(f"claimed plan of {brief.id} came back without an attempt id")
            log.info("architect_planning_claimed", planning_attempt_id=claim.planning_attempt_id)
            return (
                _PlanningAttempt(
                    brief_id=brief.id,
                    planning_attempt_id=claim.planning_attempt_id,
                    must_requirements=list(brief.content.must_requirements),
                ),
                None,
            )
        case ProductBriefPlanningAttemptOutcome.IN_PROGRESS:
            # Another architect is alive and owns this plan. Not an error and
            # not something to retry into: a second planner would create tasks
            # nobody can release and dispositions no admission counts.
            log.info("architect_planning_in_progress", rival_attempt=claim.planning_attempt_id)
            return None, live_work_settled(
                {
                    "status": "skipped",
                    "reason": "another architect owns this Product Brief plan",
                    "brief_id": brief.id,
                    "planning_attempt_id": claim.planning_attempt_id,
                }
            )
        case ProductBriefPlanningAttemptOutcome.ALREADY_ADMITTED:
            # The plan crossed the boundary. Work added to this story now is
            # ordinary work with an ordinary lifecycle, so the run proceeds
            # without an attempt: no task under an attempt, and no second
            # admission.
            log.info("architect_planning_already_admitted")
            return None, None
        case _:
            raise RuntimeError(f"unexpected planning claim outcome: {claim.outcome}")


async def _admit_plan(attempt: _PlanningAttempt, log) -> dict | None:
    """Cross the boundary once, and report the refusal when it is refused.

    Called exactly once per owned plan, after the graph has returned. `None`
    means the plan was released (or had already been); a result dict is the
    incomplete answer, which releases nothing and is not retried here — a
    second admit would give the same answer, because the missing dispositions
    are missing.
    """
    admission = await api_client.admit_product_brief_coverage(
        attempt.brief_id, attempt.planning_attempt_id
    )
    if admission.outcome is ProductBriefAdmissionOutcome.INCOMPLETE:
        # The plan is the evidence, not the LLM's account of it. A run that
        # reported success while leaving a must-requirement undisposed released
        # nothing, and says so here rather than letting the story look
        # decomposed.
        log.error(
            "architect_product_brief_incomplete",
            brief_id=attempt.brief_id,
            missing_requirement_ids=admission.missing_requirement_ids,
        )
        await _release_planning_attempt(attempt, log)
        return live_work_settled(
            {
                "status": "incomplete",
                "error": (
                    "Product Brief coverage is incomplete; nothing was released. "
                    "Undisposed must-requirements: " + ", ".join(admission.missing_requirement_ids)
                ),
                "brief_id": attempt.brief_id,
                "missing_requirement_ids": admission.missing_requirement_ids,
            }
        )
    log.info(
        "architect_product_brief_admitted",
        brief_id=attempt.brief_id,
        outcome=admission.outcome,
        released_task_ids=admission.released_task_ids,
    )
    return None


def _planning_state(attempt: _PlanningAttempt | None) -> dict:
    """The planning identity of this run — the same three keys on every run.

    Present and `None` rather than absent, so a tool that reads them out of the
    state reads one shape whether or not this story is brief-backed.
    """
    if attempt is None:
        return {"product_brief_id": None, "planning_attempt_id": None, "must_requirements": []}
    return {
        "product_brief_id": attempt.brief_id,
        "planning_attempt_id": attempt.planning_attempt_id,
        "must_requirements": attempt.must_requirements,
    }


def _requirements_briefing(attempt: _PlanningAttempt | None) -> str:
    """The requirement ids the run must dispose of, in the words the user confirmed.

    Empty for a run that is not planning under a brief: there is nothing to
    dispose of, and the instructions say nothing about a boundary that is not
    there.
    """
    if attempt is None:
        return ""
    listed = "\n".join(f"- {r.id}: {r.text}" for r in attempt.must_requirements)
    return (
        "\n\nThis story is backed by a confirmed Product Brief. Its must-requirements are:\n"
        f"{listed}\n"
        "Record exactly one disposition for EVERY id above with "
        "record_requirement_coverage — the task that covers it, or the reason it is "
        "returned. Nothing you plan is dispatched until all of them are recorded."
    )


async def _wait_for_scaffold(
    project_id: str, project: ProjectDTO, log
) -> tuple[ProjectDTO | None, str | None]:
    """Wait for scaffold to complete (DRAFT → ACTIVE).

    Returns (project, error). If error is set, caller should abort.
    """
    if project.status != ProjectStatus.DRAFT:
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
        if project.status != ProjectStatus.DRAFT:
            break
        log.debug("architect_scaffold_poll", waited=waited)

    if project.status == ProjectStatus.DRAFT:
        log.error("architect_scaffold_timeout", waited=waited)
        return project, "scaffold did not complete in time"

    log.info("architect_scaffold_ready", waited=waited)
    return project, None


async def process_architect_job(job_data: dict, redis: RedisStreamClient) -> dict:
    """Process a single architect job by running the Architect ReAct agent.

    Args:
        job_data: Job data from Redis queue (story_id, project_id, telegram_chat_id).
        redis: Redis client (unused but required by base worker signature).

    Returns:
        Result dict with status and details.
    """
    msg = validate_queued_message(ArchitectMessage, job_data)

    log = logger.bind(story_id=msg.story_id, project_id=msg.project_id)
    log.info("architect_job_started")

    # Guard: skip stories that are already past architect stage.
    # NOTE: terminal statuses (COMPLETED, FAILED, ARCHIVED) are already filtered
    # by the centralized staleness guard in _base.py. This checks non-terminal
    # statuses that are still wrong for architect (e.g. DEPLOYING).
    try:
        story = await api_client.get_story(msg.story_id)
    except Exception:
        log.warning("architect_story_not_found", story_id=msg.story_id)
        return live_work_settled({"status": "skipped", "error": "story not found"})

    story_status = story.status
    if story_status == StoryStatus.DEPLOYING:
        log.info("architect_skipping_deploying_story", status=story_status)
        return live_work_settled({"status": "skipped", "reason": f"story already {story_status}"})

    # Skip if already in_progress with tasks (duplicate message from supervisor retry)
    # But never skip reopened stories — they need re-decomposition
    if story_status == StoryStatus.IN_PROGRESS:
        existing_tasks = await api_client.get_tasks_by_story(msg.story_id)
        if existing_tasks:
            log.info("architect_skipping_already_decomposed", task_count=len(existing_tasks))
            return live_work_settled({"status": "skipped", "reason": "already decomposed"})

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
        return live_work_settled({"status": "skipped", "error": "project not found"})

    # Wait for scaffold completion (DRAFT → ACTIVE) before decomposing
    project, scaffold_err = await _wait_for_scaffold(msg.project_id, project, log)
    if scaffold_err:
        result = {"status": "failed" if project else "skipped", "error": scaffold_err}
        return live_work_unsettled(result) if project else live_work_settled(result)

    settings = get_settings()

    missing_env = missing_llm_env("architect", settings)
    if missing_env:
        log.error("architect_llm_not_configured", missing_env=missing_env)
        return live_work_unsettled(
            {"status": "failed", "error": f"{', '.join(missing_env)} not set"}
        )

    planning: _PlanningAttempt | None = None
    try:
        planning, early_result = await _claim_planning_attempt(msg.story_id, log)
        if early_result is not None:
            return early_result

        reset_task_chain()
        graph = create_architect_graph(
            model=settings.architect_llm_model,
            base_url=settings.architect_llm_base_url,
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
        user_content += _requirements_briefing(planning)

        initial_state = {
            "messages": [{"role": "user", "content": user_content}],
            "story_id": msg.story_id,
            "project_id": msg.project_id,
            "telegram_chat_id": msg.telegram_chat_id,
            **_planning_state(planning),
        }

        config = {
            "configurable": {"thread_id": str(uuid.uuid4())},
        }
        heartbeat = _planning_heartbeat(planning, log) if planning else nullcontext()
        async with heartbeat:
            result = await graph.ainvoke(initial_state, config=config)

        if planning is not None:
            refusal = await _admit_plan(planning, log)
            if refusal is not None:
                return refusal

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
        return live_work_settled({"status": "success"})

    except Exception as e:
        log.error(
            "architect_job_failed",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        if planning is not None:
            await _release_planning_attempt(planning, log)
        return live_work_unsettled({"status": "failed", "error": str(e)})


def main():
    """Entry point for running as module.

    Refuses to start without LLM config: a consumer that reads stories only to
    fail them one by one is harder to spot than a container that never comes up.
    """
    missing_env = missing_llm_env("architect", get_settings())
    if missing_env:
        raise RuntimeError(
            f"architect_llm_not_configured: {', '.join(missing_env)} not set. "
            "The Architect agent cannot decompose stories without them. "
            "Set them in .env (see .env.example)."
        )

    start_worker(
        service_name="architect",
        queue=ARCHITECT_QUEUE,
        process_fn=process_architect_job,
        group=ARCHITECT_GROUP,
    )


if __name__ == "__main__":
    main()

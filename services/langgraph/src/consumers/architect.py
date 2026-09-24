"""Architect consumer — consumes from architect:queue and decomposes stories into tasks.

Run standalone: python -m src.consumers.architect
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, nullcontext, suppress
from dataclasses import dataclass
import json
import uuid

import structlog

from shared.contracts.dto.product_brief import (
    PLANNING_ATTEMPT_HEARTBEAT_TIMEOUT_SECONDS,
    InitialSetting,
    MustRequirement,
    ProductBriefAdmissionOutcome,
    ProductBriefPlanningAttemptOutcome,
    ProductBriefRead,
    UsageExample,
)
from shared.contracts.dto.project import ProjectDTO, ProjectStatus
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.story_failure import SCAFFOLD_ERROR_KEY, StoryFailure, StoryFailureCode
from shared.contracts.queues.architect import ArchitectMessage
from shared.contracts.vocab import OwnerNotificationEvent
from shared.notifications import notify_admins_best_effort
from shared.queues import ARCHITECT_GROUP, ARCHITECT_QUEUE
from shared.redis import RedisStreamClient

from ..agents.architect.graph import create_architect_graph
from ..agents.architect.tools import reset_task_chain
from ..clients.api import api_client
from ..config.agent_llm_env import missing_llm_env
from ..config.settings import get_settings
from ._base import start_worker, validate_queued_message
from ._events import publish_story_event
from ._live_work import live_work_settled, live_work_unsettled

logger = structlog.get_logger(__name__)

SCAFFOLD_WAIT_INTERVAL = 10  # seconds between checks
SCAFFOLD_WAIT_MAX = 300  # max wait time (5 min)

#: How often the owning architect proves it is alive. Strictly below the one
#: timeout the brief contract declares, and by a whole multiple of it, so a
#: single lost heartbeat — a slow API call, one retryable failure — does not
#: hand this architect's plan to a second one while it is still planning.
PLANNING_HEARTBEAT_INTERVAL = PLANNING_ATTEMPT_HEARTBEAT_TIMEOUT_SECONDS / 3


class ReturnedRequirementsNoticeError(RuntimeError):
    """The owner could not be told which requirements the admitted plan returned.

    Raised out of the job on purpose: the queue entry stays unacknowledged, the
    consumer reclaims it once it is idle, and the replay publishes the notice
    again — through the already-decomposed skip or the `ALREADY_ADMITTED` claim.
    """


def _returned_notice_key(brief_id: str, planning_attempt_id: str) -> str:
    """Set once the owner's `po:input` accepted the notice for this admitted plan."""
    return f"architect:requirements_returned_notice:{brief_id}:{planning_attempt_id}"


@dataclass(frozen=True)
class _PlanningAttempt:
    """The plan this run owns: which brief, which attempt, what it must dispose of."""

    brief: ProductBriefRead
    brief_id: str
    planning_attempt_id: str
    must_requirements: list[MustRequirement]
    initial_settings: list[InitialSetting]
    language: str | None
    usage_examples: list[UsageExample]
    limitations: list[str]


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
    msg: ArchitectMessage, redis: RedisStreamClient, log
) -> tuple[_PlanningAttempt | None, dict | None]:
    """Decide what this run plans under, before the graph is invoked.

    Returns `(attempt, early_result)`. `(None, None)` is the ordinary run: the
    story has no brief, or its brief was already admitted, and either way this
    run creates no task under an attempt and admits nothing. An `early_result`
    means another architect owns the plan and this run must not touch it.
    """
    brief = await api_client.get_product_brief_by_story(msg.story_id)
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
                    brief=brief,
                    brief_id=brief.id,
                    planning_attempt_id=claim.planning_attempt_id,
                    must_requirements=list(brief.content.must_requirements),
                    initial_settings=list(brief.content.initial_settings),
                    language=brief.content.language,
                    usage_examples=list(brief.content.usage_examples),
                    limitations=list(brief.content.limitations),
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
            # admission. The owner may still be owed the notice of what that
            # plan returned, when the run that admitted it failed to publish it.
            log.info("architect_planning_already_admitted")
            await _notify_returned_requirements_of_admitted_brief(msg, redis, log, brief)
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


def _returned_notice_text(brief: ProductBriefRead, returned: list) -> str:
    """What PO is told: each returned requirement, the user's words and the reason."""
    requirements = {r.id: r for r in brief.content.must_requirements}
    lines = [
        f"The confirmed Product Brief {brief.id} was planned, but {len(returned)} of its "
        "must-requirements were returned instead of planned: they will NOT be built in this "
        "story. The rest of the story is being built."
    ]
    if brief.content.language:
        lines.append(f"User's language: {brief.content.language}.")
    for row in returned:
        requirement = requirements.get(row.requirement_id)
        lines.append(f"- {row.requirement_id}: {requirement.text if requirement else ''}")
        if requirement and requirement.user_wording:
            lines.append(f"  the user's words: {requirement.user_wording}")
        lines.append(f"  reason: {row.returned_reason}")
    return "\n".join(lines)


async def _notify_returned_requirements(  # noqa: PLR0913 — one admitted plan's recipient
    brief: ProductBriefRead,
    planning_attempt_id: str,
    *,
    story_id: str,
    project_id: str,
    telegram_chat_id: str,
    redis: RedisStreamClient,
    log,
) -> None:
    """Tell the owner, once, which must-requirements the admitted plan returned.

    Nothing is published when the plan returned nothing, or when the marker says
    `po:input` already accepted this notice. A recipient that never resolved is
    refused with a log and an admin alert: replaying the job changes nothing.
    Any other failure raises `ReturnedRequirementsNoticeError`, which leaves the
    queue entry unacknowledged so the replay publishes it.
    """
    key = _returned_notice_key(brief.id, planning_attempt_id)
    try:
        rows = await api_client.list_requirement_coverage(brief.id)
        returned = [
            row
            for row in rows
            if row.planning_attempt_id == planning_attempt_id and row.returned_reason
        ]
        if not returned or await redis.redis.exists(key):
            return
        returned_ids = [row.requirement_id for row in returned]
        if not telegram_chat_id:
            log.error(
                "architect_requirements_returned_without_recipient",
                brief_id=brief.id,
                returned_requirement_ids=returned_ids,
            )
            await notify_admins_best_effort(
                f"Story {story_id}: the architect returned must-requirements "
                f"{', '.join(returned_ids)} of brief {brief.id}, but the job carries no "
                "Telegram recipient, so the owner cannot be told; retrying changes nothing",
                level="error",
                component="architect",
                story_id=story_id,
                project_id=project_id,
            )
            return
        await publish_story_event(
            redis,
            telegram_chat_id=telegram_chat_id,
            event=OwnerNotificationEvent.STORY_REQUIREMENTS_RETURNED,
            text=_returned_notice_text(brief, returned),
            story_id=story_id,
            project_id=project_id,
        )
        await redis.redis.set(key, "1")
    except Exception as e:
        log.error(
            "architect_requirements_returned_notice_failed",
            brief_id=brief.id,
            planning_attempt_id=planning_attempt_id,
            error=str(e),
            error_type=type(e).__name__,
        )
        raise ReturnedRequirementsNoticeError(
            f"notice of requirements returned by plan {planning_attempt_id} of brief "
            f"{brief.id} was not published: {e}"
        ) from e
    log.info(
        "architect_requirements_returned_notice_published",
        brief_id=brief.id,
        returned_requirement_ids=returned_ids,
    )


async def _notify_returned_requirements_of_admitted_brief(
    msg: ArchitectMessage, redis: RedisStreamClient, log, brief: ProductBriefRead | None = None
) -> None:
    """The replay of the notice, for a run that finds the plan already admitted."""
    if brief is None:
        brief = await api_client.get_product_brief_by_story(msg.story_id)
    if brief is None or brief.coverage_admitted_at is None or brief.planning_attempt_id is None:
        return
    await _notify_returned_requirements(
        brief,
        brief.planning_attempt_id,
        story_id=msg.story_id,
        project_id=msg.project_id,
        telegram_chat_id=msg.telegram_chat_id,
        redis=redis,
        log=log,
    )


async def _notify_returned_requirements_of_plan(
    planning: _PlanningAttempt | None, msg: ArchitectMessage, redis: RedisStreamClient, log
) -> None:
    """The notice for the plan this run just admitted; nothing for a run without one."""
    if planning is None:
        return
    await _notify_returned_requirements(
        planning.brief,
        planning.planning_attempt_id,
        story_id=msg.story_id,
        project_id=msg.project_id,
        telegram_chat_id=msg.telegram_chat_id,
        redis=redis,
        log=log,
    )


async def _skip_already_decomposed(
    msg: ArchitectMessage, story_status: StoryStatus, redis: RedisStreamClient, log
) -> dict | None:
    """The skip result for an in-progress story that already has tasks, else `None`.

    The replay of a job whose notice failed after admission lands here: the
    story already moved on, but the owner is still owed the notice.
    """
    if story_status != StoryStatus.IN_PROGRESS:
        return None
    existing_tasks = await api_client.get_tasks_by_story(msg.story_id)
    if not existing_tasks:
        return None
    log.info("architect_skipping_already_decomposed", task_count=len(existing_tasks))
    await _notify_returned_requirements_of_admitted_brief(msg, redis, log)
    return live_work_settled({"status": "skipped", "reason": "already decomposed"})


def _planning_state(attempt: _PlanningAttempt | None) -> dict:
    """The planning identity of this run — the same three keys on every run.

    Present and `None` rather than absent, so a tool that reads them out of the
    state reads one shape whether or not this story is brief-backed.
    """
    if attempt is None:
        return {
            "product_brief_id": None,
            "planning_attempt_id": None,
            "must_requirements": [],
            "initial_settings": [],
        }
    return {
        "product_brief_id": attempt.brief_id,
        "planning_attempt_id": attempt.planning_attempt_id,
        "must_requirements": attempt.must_requirements,
        "initial_settings": attempt.initial_settings,
    }


def _requirements_briefing(attempt: _PlanningAttempt | None) -> str:
    """The requirement ids the run must dispose of, in the words the user confirmed.

    Empty for a run that is not planning under a brief: there is nothing to
    dispose of, and the instructions say nothing about a boundary that is not
    there.
    """
    if attempt is None:
        return ""
    listed = "\n".join(
        f"- {r.id}: {r.text}"
        + ("" if r.user_facing else " (not user-facing: the user sends nothing for it)")
        for r in attempt.must_requirements
    )
    language = f" (user's language: {attempt.language})" if attempt.language else ""
    briefing = (
        f"\n\nThis story is backed by a confirmed Product Brief{language}. "
        "Its must-requirements are:\n"
        f"{listed}\n"
        "Record exactly one disposition for EVERY id above with "
        "record_requirement_coverage — the task that covers it, or the reason it is "
        "returned. Nothing you plan is dispatched until all of them are recorded."
    )
    return (
        briefing
        + _usage_briefing(attempt)
        + _limitations_briefing(attempt)
        + _settings_briefing(attempt)
    )


def _usage_briefing(attempt: _PlanningAttempt) -> str:
    """How the user confirmed each requirement is used, grouped by requirement, verbatim.

    Grouped in requirement order rather than in the order the user was shown
    them, because every group is planned — or returned — as one requirement.
    Empty for a brief stored before usage examples existed.
    """
    if not attempt.usage_examples:
        return ""
    order = [r.id for r in attempt.must_requirements]
    order += [e.requirement_id for e in attempt.usage_examples if e.requirement_id not in order]
    lines: list[str] = []
    for requirement_id in dict.fromkeys(order):
        examples = [e for e in attempt.usage_examples if e.requirement_id == requirement_id]
        if not examples:
            continue
        lines.append(f"[{requirement_id}]")
        for example in examples:
            lines.append(f"  - the user sends: {example.user_sends}")
            lines.append(f"    the product answers: {example.product_answers}")
    listed = "\n".join(lines)
    return (
        "\n\nThe user confirmed how each requirement is used, in their own words:\n"
        f"{listed}\n"
        "Plan exactly these uses. Turn every usage example of a requirement you plan into "
        "its own acceptance criterion stated through what QA can do and ending with "
        "(requirement <id>); an upload example is checked through its observable or marked "
        "not QA-verifiable, never dropped. When a requirement's examples and the limitations "
        "leave an input undefined, return that requirement with returned_reason naming the "
        "undefined input instead of planning a narrower version. Every task states that the "
        "product never stores input it does not recognize as a different kind of record: it "
        "asks the user back what they meant."
    )


def _limitations_briefing(attempt: _PlanningAttempt) -> str:
    """The limitations and trade-offs the user confirmed, one sentence each."""
    if not attempt.limitations:
        return ""
    listed = "\n".join(f"- {limitation}" for limitation in attempt.limitations)
    return (
        "\n\nLimitations and trade-offs the user confirmed:\n"
        f"{listed}\n"
        "They are decisions, not gaps: plan within them, and read them when deciding "
        "whether a requirement's input is settled."
    )


def _settings_briefing(attempt: _PlanningAttempt) -> str:
    """The typed values the confirmed product starts life with.

    They are named here rather than described, because the platform writes them
    into the deployed product itself, through the product's own settings write
    path, exactly as the user confirmed them. For a service-owned key, what the
    architect owes is the declaration that makes it writable at all. A selected
    installed package can instead own the prefixed declaration and its setting
    seed.
    """
    if not attempt.initial_settings:
        return ""
    listed = "\n".join(
        f"- {s.key} (scope: {s.scope.value}"
        + (f", subject_id: {s.subject_id}" if s.subject_id is not None else "")
        + f") = {json.dumps(s.value, ensure_ascii=False)}"
        for s in attempt.initial_settings
    )
    return (
        "\n\nThe same confirmed brief carries the typed settings this product starts "
        "life with:\n"
        f"{listed}\n"
        "Do NOT plan work that writes these values: the platform writes them into the "
        "deployed product after deploy, through the product's own settings write path. "
        "For an ordinary service-owned setting, your plan must make the key writable: "
        "declare it in the product's own services/<service>/manifest.yaml "
        "settings_schema, with a JSON Schema the value shown above satisfies, and have "
        "the product read the setting where it uses it. When your capability-shape "
        "decision instead selects an installed kit package that owns a confirmed "
        "prefixed package-owned setting and declares its setting seed, rely on that "
        "package declaration and the platform's existing settings write. The package "
        "install generates the registry entry, and successful POST /settings/set invokes "
        "the package's idempotent seed in the same transaction. Do NOT ask the product "
        "to duplicate the key in a service manifest, author a DB trigger, add startup "
        "polling, or add product-owned seed code. In either case, the task must run the "
        "generator and verify each exact "
        "key reaches the generated settings registry (for a backend, "
        "services/backend/src/generated/settings_schemas.py), then test generated "
        "POST /settings/set and POST /settings/get with SETTINGS_WRITE_CAPABILITY. A key "
        "the manifest does not declare, or does not reach that registry, is refused by "
        "the product and the confirmed value never arrives. Cover that work in the tasks "
        "you create."
    )


def _recorded_scaffold_error(project: ProjectDTO) -> str | None:
    """The failure the scaffolder recorded on the project, if it recorded one."""
    error = (project.config or {}).get(SCAFFOLD_ERROR_KEY)
    return None if error is None else str(error)


async def _wait_for_scaffold(
    project_id: str, project: ProjectDTO, log
) -> tuple[ProjectDTO | None, str | None, StoryFailure | None]:
    """Wait for scaffold to complete (DRAFT → ACTIVE).

    Returns (project, error, failure). If error is set, caller should abort;
    ``failure`` then says why the story itself has to stop. A recorded
    ``scaffold_error`` ends the wait at once — ``scaffold_trigger`` never retries
    that project, so waiting out the window would only hide the cause.
    """
    if project.status != ProjectStatus.DRAFT:
        return project, None, None

    log.info("architect_waiting_for_scaffold")
    waited = 0
    while True:
        recorded = _recorded_scaffold_error(project)
        if recorded is not None:
            log.error("architect_scaffold_failed", waited=waited, scaffold_error=recorded)
            failure = StoryFailure(
                code=StoryFailureCode.SCAFFOLD_FAILED, source="architect", detail=recorded
            )
            return project, "scaffold failed", failure
        if waited >= SCAFFOLD_WAIT_MAX:
            break
        await asyncio.sleep(SCAFFOLD_WAIT_INTERVAL)
        waited += SCAFFOLD_WAIT_INTERVAL
        project = await api_client.get_project(project_id)
        if not project:
            log.warning("architect_project_deleted_during_scaffold_wait")
            return None, "project deleted during scaffold wait", None
        if project.status != ProjectStatus.DRAFT:
            log.info("architect_scaffold_ready", waited=waited)
            return project, None, None
        log.debug("architect_scaffold_poll", waited=waited)

    log.error("architect_scaffold_timeout", waited=waited)
    failure = StoryFailure(
        code=StoryFailureCode.SCAFFOLD_TIMEOUT,
        source="architect",
        detail=(
            f"the project repository was still not ready after {waited} seconds "
            "and the scaffolder recorded no error"
        ),
    )
    return project, "scaffold did not complete in time", failure


async def _stop_story_on_scaffold_failure(story_id: str, failure: StoryFailure, log) -> bool:
    """Take the story out of ``in_progress`` with the reason it cannot go on.

    A recorded scaffold error is final — nothing retries that project — so the
    story fails. A timeout with no recorded error may still be a scaffold that
    is merely slow, so the story is parked for a person instead, which they can
    resume. Either way the reason and the owner's notice are written by the API
    together with the transition. A refused transition (the story already moved)
    is logged, never raised: it must not turn this job into a replayed one.
    """
    action = "fail" if failure.code is StoryFailureCode.SCAFFOLD_FAILED else "human-review"
    try:
        await api_client.stop_story(story_id, action, failure, actor="architect")
    except Exception as exc:
        log.error(
            "architect_scaffold_story_stop_failed",
            action=action,
            failure_code=failure.code.value,
            error=str(exc),
        )
        return False
    log.info("architect_scaffold_story_stopped", action=action, failure_code=failure.code.value)
    return True


async def _await_scaffold_or_stop(
    msg: ArchitectMessage, project: ProjectDTO, log
) -> tuple[ProjectDTO | None, dict | None]:
    """The ready project, or the job result when the scaffold never became ready.

    A scaffold that failed or timed out also stops the story, with its reason,
    so the story does not stay ``in_progress`` with nothing behind it. Once the
    story is stopped the job is settled: a replay would find nothing to plan.
    """
    project, scaffold_err, scaffold_failure = await _wait_for_scaffold(msg.project_id, project, log)
    if not scaffold_err:
        return project, None
    stopped = scaffold_failure is not None and await _stop_story_on_scaffold_failure(
        msg.story_id, scaffold_failure, log
    )
    result = {"status": "failed" if project else "skipped", "error": scaffold_err}
    if project and not stopped:
        return project, live_work_unsettled(result)
    return project, live_work_settled(result)


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
    skipped = await _skip_already_decomposed(msg, story_status, redis, log)
    if skipped is not None:
        return skipped

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
    project, scaffold_result = await _await_scaffold_or_stop(msg, project, log)
    if scaffold_result is not None:
        return scaffold_result

    settings = get_settings()

    missing_env = missing_llm_env("architect", settings)
    if missing_env:
        log.error("architect_llm_not_configured", missing_env=missing_env)
        return live_work_unsettled(
            {"status": "failed", "error": f"{', '.join(missing_env)} not set"}
        )

    planning: _PlanningAttempt | None = None
    try:
        planning, early_result = await _claim_planning_attempt(msg, redis, log)
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

        # Last, after the story moved on: a failure here is replayed, and the
        # replay must not find a plan that is still waiting to start.
        await _notify_returned_requirements_of_plan(planning, msg, redis, log)

        log.info(
            "architect_job_success",
            message_count=len(result.get("messages", [])),
        )
        return live_work_settled({"status": "success"})

    except ReturnedRequirementsNoticeError:
        # The plan is admitted and nothing is released again; only the notice is
        # owed. Leaving the entry unacknowledged is what replays it.
        raise
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

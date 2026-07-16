"""Pipeline supervisor — detect stuck stories/tasks, retry or escalate."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

from pydantic import ValidationError
import structlog

from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import DeployRunResult, QARunResult
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.architect import ArchitectMessage
from shared.contracts.queues.deploy import DeployMessage, DeployOutcome, DeployTrigger
from shared.contracts.queues.engineering import EngineeringMessage
from shared.contracts.queues.qa import QAMessage, QAOutcome
from shared.notifications import notify_admins_best_effort
from shared.queues import ARCHITECT_QUEUE, DEPLOY_QUEUE, ENGINEERING_QUEUE, QA_QUEUE
from shared.redis_client import RedisStreamClient

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient

from .. import startup

logger = structlog.get_logger(__name__)

STORY_RETRY_KEY_PREFIX = "story:architect_retries:"
DEPLOY_RETRY_KEY_PREFIX = "deploy:retries:"


def _max_deploy_retries() -> int:
    return startup.get_config().get_int("deploy.max_deploy_retries")


def _max_deploy_fix_attempts() -> int:
    return startup.get_config().get_int("deploy.max_deploy_fix_attempts")


def _deploy_retry_ttl() -> int:
    return startup.get_config().get_int("deploy.deploy_retry_ttl")


def _story_stuck_threshold() -> int:
    return startup.get_config().get_int("supervisor.story_stuck_threshold_minutes")


def _task_stuck_threshold() -> int:
    return startup.get_config().get_int("supervisor.task_stuck_threshold_minutes")


def _max_architect_retries() -> int:
    return startup.get_config().get_int("supervisor.story_max_architect_retries")


def _story_retry_ttl() -> int:
    return startup.get_config().get_int("supervisor.story_retry_ttl")


def _parse_datetime(value: str | datetime) -> datetime:
    """Parse ISO datetime string or pass through datetime objects.

    Handles both Z and +00:00 suffixes for string inputs.
    """
    if isinstance(value, datetime):
        return value
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


async def supervise_stuck_stories(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    *,
    _retry_counts: dict[str, int] | None = None,
) -> dict[str, int]:
    """Detect stories stuck in 'created' with no tasks and retry architect.

    Retry counts are persisted in Redis so they survive scheduler restarts.

    Returns dict with 'retried' and 'failed' counts.
    """
    stories = await api_client.get_stories_by_status(StoryStatus.CREATED)
    retried = 0
    failed = 0

    # Build set of projects that already have an active story
    active_stories = await api_client.get_stories_by_status(StoryStatus.IN_PROGRESS)
    active_projects = {str(s.project_id) for s in active_stories}

    now = datetime.now(UTC)
    redis = redis_client._redis

    for story in stories:
        story_id = story.id
        project_id = str(story.project_id)
        created_at = _parse_datetime(story.created_at)
        age_minutes = (now - created_at).total_seconds() / 60

        if age_minutes < _story_stuck_threshold():
            continue

        # Skip if project already has an active story (sequential processing)
        if project_id in active_projects:
            continue

        # Only retry if architect hasn't created any tasks yet
        tasks = await api_client.get_tasks_by_story(story_id)
        if tasks:
            continue

        log = logger.bind(story_id=story_id, age_minutes=round(age_minutes, 1))

        retry_key = f"{STORY_RETRY_KEY_PREFIX}{story_id}"
        raw = await redis.get(retry_key)
        current_retries = int(raw) if raw else 0

        if current_retries >= _max_architect_retries():
            log.error(
                "story_terminal_failure",
                reason="architect_retries_exhausted",
                retries=current_retries,
            )
            await api_client.fail_story(story_id)
            await redis.delete(retry_key)
            failed += 1
            continue

        # Retry: republish to architect:queue (StoryDTO has no user_id field)
        arch_msg = ArchitectMessage(
            story_id=story_id,
            project_id=project_id,
            user_id="",
        )
        await redis_client.publish_message(ARCHITECT_QUEUE, arch_msg)
        await redis.set(retry_key, current_retries + 1, ex=_story_retry_ttl())

        log.warning(
            "story_stuck_retry",
            retry_attempt=current_retries + 1,
            max_retries=_max_architect_retries(),
        )
        retried += 1

    return {"retried": retried, "failed": failed}


async def supervise_failed_tasks(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """Detect failed tasks and retry or escalate to waiting_human_review.

    FAILED status means technical failure (crash, OOM, timeout) — the worker
    never explicitly gave up. Supervisor retries if iterations remain, otherwise
    transitions to WAITING_HUMAN_REVIEW (same as gave_up — needs human).

    Returns dict with 'retried' and 'escalated' counts.
    """
    tasks = await api_client.get_tasks_by_status(TaskStatus.FAILED)
    retried = 0
    escalated = 0

    for task in tasks:
        task_id = task.id
        story_id = task.story_id

        # Skip standalone tasks (not part of a story)
        if not story_id:
            continue

        current_iter = task.current_iteration
        max_iter = task.max_iterations
        log = logger.bind(task_id=task_id, story_id=story_id, iteration=current_iter)

        if current_iter < max_iter:
            # Retry: failed → backlog → todo, bump iteration
            await api_client.transition_task(task_id, TaskStatus.BACKLOG, "supervisor")
            await api_client.transition_task(task_id, TaskStatus.TODO, "supervisor")
            await api_client.update_task(task_id, {"current_iteration": current_iter + 1})
            log.warning(
                "task_retry",
                new_iteration=current_iter + 1,
                max_iterations=max_iter,
            )
            retried += 1
        else:
            # Retries exhausted → escalate to human (same as gave_up)
            log.warning(
                "task_retries_exhausted",
                reason="escalating_to_human",
            )
            try:
                await api_client.transition_task(
                    task_id, TaskStatus.WAITING_HUMAN_REVIEW, "supervisor"
                )
            except Exception:
                log.warning("task_whr_transition_failed", task_id=task_id, exc_info=True)

            if story_id:
                try:
                    await api_client.transition_story(story_id, StoryStatus.WAITING_HUMAN_REVIEW)
                except Exception:
                    log.warning(
                        "story_whr_on_retries_exhausted_failed",
                        story_id=story_id,
                        exc_info=True,
                    )

            escalated += 1

    return {"retried": retried, "escalated": escalated}


async def supervise_stuck_tasks(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """Detect tasks stuck in in_dev and fail them.

    Failed tasks will be picked up by supervise_failed_tasks for retry.
    Returns dict with 'timed_out' count.
    """
    tasks = await api_client.get_tasks_by_status(TaskStatus.IN_DEV)
    timed_out = 0
    now = datetime.now(UTC)

    for task in tasks:
        task_id = task.id
        updated_at = _parse_datetime(task.updated_at)
        age_minutes = (now - updated_at).total_seconds() / 60

        if age_minutes < _task_stuck_threshold():
            continue

        log = logger.bind(task_id=task_id, age_minutes=round(age_minutes, 1))
        log.warning("task_stuck_timeout", threshold_minutes=_task_stuck_threshold())

        await api_client.transition_task(task_id, TaskStatus.FAILED, "supervisor")
        timed_out += 1

    return {"timed_out": timed_out}


async def supervise_deploying_stories(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """Poll DEPLOYING stories and route based on deploy run outcome.

    Reads run.result.deploy_outcome set by the deploy worker:
    - SUCCESS → story TESTING, publish QAMessage
    - SMOKE_FAILURE / CODE_FIX → story IN_PROGRESS, redispatch to engineering
    - RETRY → increment retry counter, re-publish DeployMessage or FAILED
    - GIVE_UP → story FAILED, notify admins

    Returns dict with counts of actions taken.
    """
    stories = await api_client.get_stories_by_status(StoryStatus.DEPLOYING)
    if not stories:
        return {"tested": 0, "retried": 0, "redispatched": 0, "failed": 0}

    tested = 0
    retried = 0
    redispatched = 0
    failed = 0
    redis = redis_client._redis

    for story in stories:
        story_id = story.id
        project_id = str(story.project_id)
        log = logger.bind(story_id=story_id, project_id=project_id)

        # Find latest deploy run for this story
        try:
            run = await api_client.get_latest_run_by_story(story_id, run_type="deploy")
        except ValidationError as exc:
            await _fail_story_on_invalid_result(
                api_client, story_id, project_id, "deploy", exc, log
            )
            failed += 1
            continue
        if run is None:
            continue

        # Skip runs still in progress
        if run.status in (RunStatus.QUEUED, RunStatus.RUNNING):
            continue

        # Only a superseded (CANCELLED) run reaches here without a result; a
        # terminal run that lost its outcome would have failed validation above.
        if run.result is None:
            log.info("deploy_run_superseded_skip", run_id=run.id, run_status=run.status.value)
            continue

        outcome = run.result.deploy_outcome

        if outcome == DeployOutcome.SUCCESS:
            handed_off = await _handle_deploy_success_story(
                api_client, redis_client, story_id, project_id, run.result, log
            )
            if handed_off:
                tested += 1
            else:
                failed += 1

        elif outcome in (DeployOutcome.CODE_FIX, DeployOutcome.SMOKE_FAILURE):
            dispatched = await _handle_deploy_code_fix(
                api_client, redis_client, story_id, project_id, run, run.result, log
            )
            if dispatched:
                redispatched += 1
            else:
                failed += 1

        elif outcome == DeployOutcome.RETRY:
            was_retried = await _handle_deploy_retry(
                api_client, redis_client, redis, story_id, project_id, run, log
            )
            if was_retried:
                retried += 1
            else:
                failed += 1

        elif outcome in (
            DeployOutcome.GIVE_UP,
            DeployOutcome.WAITING_FOR_USER_SECRET,
            DeployOutcome.ALLOCATION_MISSING,
            DeployOutcome.ENVIRONMENT_CONTRACT_INVALID,
            DeployOutcome.ENVIRONMENT_RESOLUTION_FAILED,
        ):
            await _handle_deploy_give_up(api_client, story_id, project_id, run, log)
            failed += 1

    return {
        "tested": tested,
        "retried": retried,
        "redispatched": redispatched,
        "failed": failed,
    }


async def _handle_deploy_success_story(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    result: DeployRunResult,
    log: structlog.stdlib.BoundLogger,
) -> bool:
    """Deploy succeeded — transition story to TESTING, create QA run, publish QA message.

    Returns True if the story was handed off to QA, False if QA's preconditions
    were not met (handled as a visible failure).
    """
    deployed_url = result.deployed_url
    application_id = result.application_id
    bot_username = result.bot_username

    # A QA handoff needs both the deployed URL and the application id. `application_id`
    # is legitimately optional on a DeployRunResult (a standalone deploy, or one where
    # the app record couldn't be resolved), so validate the precondition here — before
    # mutating story/run state — and route a success that can't reach QA to a visible
    # failure instead of crashing the tick mid-handoff.
    if deployed_url is None or application_id is None:
        missing = ", ".join(
            name
            for name, value in (("deployed_url", deployed_url), ("application_id", application_id))
            if value is None
        )
        log.error("deploy_success_missing_handoff_fields", missing=missing)
        await api_client.fail_story(story_id)
        await _notify_admin_failure(
            story_id, project_id, f"deploy reported success but missing {missing} — cannot run QA"
        )
        return False

    # QA validates the story against the repository's criteria, so resolve them
    # here and carry them on the message. Same reason as the fields above: a
    # story whose criteria are missing must not reach TESTING with a QA run that
    # can only error out.
    acceptance_criteria = await _resolve_acceptance_criteria(api_client, project_id, log)
    if acceptance_criteria is None:
        await api_client.fail_story(story_id)
        await _notify_admin_failure(
            story_id,
            project_id,
            "deploy succeeded but the project's repository has no acceptance criteria — "
            "cannot run QA",
        )
        return False

    await api_client.transition_story(story_id, "test")

    # Create QA run so the consumer can store its outcome
    qa_run_id = f"qa-{uuid.uuid4().hex[:8]}"
    await api_client.create_run(
        {
            "id": qa_run_id,
            "type": RunType.QA.value,
            "project_id": project_id,
            "story_id": story_id,
            "status": RunStatus.QUEUED.value,
        }
    )

    await redis_client.publish_message(
        QA_QUEUE,
        QAMessage(
            story_id=story_id,
            project_id=project_id,
            user_id="",
            deployed_url=deployed_url,
            application_id=application_id,
            acceptance_criteria=acceptance_criteria,
            bot_username=bot_username,
            run_id=qa_run_id,
        ),
    )
    log.info("deploy_supervisor_qa_handoff", deployed_url=deployed_url, qa_run_id=qa_run_id)
    return True


async def _resolve_acceptance_criteria(
    api_client: SchedulerAPIClient,
    project_id: str,
    log: structlog.stdlib.BoundLogger,
) -> str | None:
    """Read the project's QA criteria, or None if there are none to run."""
    repo = await api_client.get_primary_repository(project_id)
    if repo is None:
        log.error("deploy_success_no_primary_repository")
        return None

    criteria = (repo.acceptance_criteria or "").strip()
    if not criteria:
        log.error("deploy_success_no_acceptance_criteria", repo_id=repo.id)
        return None
    return criteria


async def _handle_deploy_code_fix(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    run,
    result: DeployRunResult,
    log: structlog.stdlib.BoundLogger,
) -> bool:
    """Deploy failed with CODE_FIX — redispatch to engineering if retries remain.

    Returns True if redispatched, False if retries exhausted.
    """
    attempt = result.deploy_fix_attempt
    if attempt >= _max_deploy_fix_attempts():
        log.warning(
            "deploy_fix_retries_exhausted",
            attempt=attempt,
            max=_max_deploy_fix_attempts(),
        )
        await api_client.fail_story(story_id)
        await _notify_admin_failure(run.id, project_id, "deploy fix retries exhausted")
        return False

    # Transition story back to IN_PROGRESS
    await api_client.transition_story(story_id, "start")

    error_details = result.error_details or "unknown deploy error"
    fix_task_id = f"eng-deploy-fix-{run.id}-{attempt + 1}"

    # Create a run record for the fix task
    try:
        await api_client.create_run(
            {
                "id": fix_task_id,
                "type": RunType.ENGINEERING.value,
                "project_id": project_id,
                "story_id": story_id,
                "status": RunStatus.QUEUED.value,
            }
        )
    except Exception:
        log.warning("deploy_fix_run_create_failed", fix_task_id=fix_task_id, exc_info=True)

    fix_msg = EngineeringMessage(
        task_id=fix_task_id,
        project_id=project_id,
        user_id="",
        action="fix",
        description=(
            f"Deploy failed — fix the code so containers start cleanly.\n\n"
            f"Error: {error_details}\n\n"
            f"Run the service locally or check imports/dependencies before pushing."
        ),
        skip_deploy=False,
        story_id=story_id,
        deploy_fix_attempt=attempt + 1,
    )

    await redis_client.publish_message(ENGINEERING_QUEUE, fix_msg)
    log.info("deploy_supervisor_code_fix", fix_task_id=fix_task_id, attempt=attempt + 1)
    return True


async def _handle_deploy_retry(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    redis,
    story_id: str,
    project_id: str,
    run,
    log: structlog.stdlib.BoundLogger,
) -> bool:
    """Deploy failed with RETRY — re-publish deploy message if retries remain.

    Returns True if retried, False if max retries exceeded.
    """
    retry_key = f"{DEPLOY_RETRY_KEY_PREFIX}{story_id}"
    attempts = await redis.incr(retry_key)
    await redis.expire(retry_key, _deploy_retry_ttl())

    if attempts >= _max_deploy_retries():
        log.warning(
            "deploy_max_retries_exceeded",
            story_id=story_id,
            attempts=attempts,
            max_retries=_max_deploy_retries(),
        )
        await api_client.fail_story(story_id)
        await redis.delete(retry_key)
        await _notify_admin_failure(run.id, project_id, f"deploy retries exhausted ({attempts})")
        return False

    # Re-publish deploy message for retry
    new_run_id = f"deploy-retry-{uuid.uuid4().hex[:8]}"
    await api_client.create_run(
        {
            "id": new_run_id,
            "type": RunType.DEPLOY.value,
            "project_id": project_id,
            "story_id": story_id,
            "status": RunStatus.QUEUED.value,
            "run_metadata": {"triggered_by": "supervisor_retry", "attempt": attempts},
        }
    )

    deploy_msg = DeployMessage(
        task_id=new_run_id,
        project_id=project_id,
        user_id="",
        story_id=story_id,
        triggered_by=DeployTrigger.WEBHOOK,
        action="feature",
    )
    await redis_client.publish_message(DEPLOY_QUEUE, deploy_msg)
    log.info(
        "deploy_supervisor_retry",
        new_run_id=new_run_id,
        attempt=attempts,
        max_retries=_max_deploy_retries(),
    )
    return True


async def _handle_deploy_give_up(
    api_client: SchedulerAPIClient,
    story_id: str,
    project_id: str,
    run,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Deploy failed with GIVE_UP — terminal failure, admin notified."""
    log.warning("deploy_supervisor_give_up", run_id=run.id)
    await api_client.fail_story(story_id)
    error_msg = (run.result.error_details if run.result else None) or "unknown error"
    await _notify_admin_failure(run.id, project_id, error_msg)


async def _fail_story_on_invalid_result(
    api_client: SchedulerAPIClient,
    story_id: str,
    project_id: str,
    run_type: str,
    exc: ValidationError,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Route a story whose latest run has an unparseable result to a terminal, visible state.

    A legacy or corrupt `run.result` would otherwise fail validation on every poll and
    wedge the story forever. Fail it once, loudly, and notify admins — no silent skip,
    no infinite retry.
    """
    log.error("run_result_invalid", run_type=run_type, error=str(exc))
    await api_client.fail_story(story_id)
    await _notify_admin_failure(story_id, project_id, f"invalid {run_type} run result: {exc}")


async def _notify_admin_failure(run_id: str, project_id: str, error: str) -> None:
    """Notify after a terminal failure has already been committed."""
    await notify_admins_best_effort(
        f"Deploy GIVE_UP for run {run_id} (project {project_id}):\n{error[:500]}",
        level="error",
        component="supervisor",
        run_id=run_id,
        project_id=project_id,
    )


# ---------------------------------------------------------------------------
# QA supervision — TESTING stories
# ---------------------------------------------------------------------------

MAX_QA_LOOPS = 2  # max QA→Engineering cycles before story is marked failed


async def supervise_testing_stories(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """Poll TESTING stories and route based on QA run outcome.

    Reads run.result.qa_outcome set by the QA consumer:
    - PASSED → story COMPLETED
    - FAILED → create fix task, story IN_PROGRESS, redispatch to engineering
    - EXHAUSTED → story FAILED
    - ERROR → story FAILED

    Returns dict with counts of actions taken.
    """
    stories = await api_client.get_stories_by_status(StoryStatus.TESTING)
    if not stories:
        return {"completed": 0, "redispatched": 0, "failed": 0}

    completed = 0
    redispatched = 0
    failed = 0

    for story in stories:
        story_id = story.id
        project_id = str(story.project_id)
        log = logger.bind(story_id=story_id, project_id=project_id)

        # Find latest QA run for this story
        try:
            run = await api_client.get_latest_run_by_story(story_id, run_type="qa")
        except ValidationError as exc:
            await _fail_story_on_invalid_result(api_client, story_id, project_id, "qa", exc, log)
            failed += 1
            continue
        if run is None:
            continue

        # Skip runs still in progress
        if run.status in (RunStatus.QUEUED, RunStatus.RUNNING):
            continue

        # A terminal QA run always carries a result (validation enforces it);
        # None here only means a superseded/non-terminal run — skip it.
        if run.result is None:
            log.info("qa_run_superseded_skip", run_id=run.id, run_status=run.status.value)
            continue

        outcome = run.result.qa_outcome

        if outcome == QAOutcome.PASSED:
            await api_client.transition_story(story_id, "complete")
            log.info("qa_supervisor_completed", run_id=run.id)
            completed += 1

        elif outcome == QAOutcome.FAILED:
            dispatched = await _handle_qa_failed(
                api_client, redis_client, story_id, project_id, run.result, log
            )
            if dispatched:
                redispatched += 1
            else:
                failed += 1

        elif outcome in (QAOutcome.EXHAUSTED, QAOutcome.ERROR):
            await api_client.fail_story(story_id)
            log.warning("qa_supervisor_failed", outcome=outcome.value, run_id=run.id)
            failed += 1

    return {"completed": completed, "redispatched": redispatched, "failed": failed}


async def _handle_qa_failed(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    result: QARunResult,
    log: structlog.stdlib.BoundLogger,
) -> bool:
    """QA failed — create fix task and redispatch to engineering.

    Returns True if redispatched, False if something went wrong.
    """
    summary = result.summary or "QA testing failed"
    failed_checks = result.failed_checks

    issues_text = "\n".join(f"- {c.name}: {c.detail}" for c in failed_checks)
    if not issues_text:
        issues_text = summary

    fix_description = (
        f"QA testing found issues after deploy. Fix the following:\n\n"
        f"{issues_text}\n\n"
        f"QA summary: {summary}"
    )

    await api_client.create_task(
        {
            "project_id": project_id,
            "story_id": story_id,
            "title": f"QA fix: {summary[:80]}",
            "type": "fix",
            "status": TaskStatus.TODO.value,
            "description": fix_description,
        }
    )

    # Transition story back to IN_PROGRESS for engineering
    await api_client.transition_story(story_id, "start")

    log.info("qa_supervisor_fix_task_created", story_id=story_id)
    return True

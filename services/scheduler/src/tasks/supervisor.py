"""Pipeline supervisor — detect stuck stories/tasks, retry or escalate."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from typing import TYPE_CHECKING
import uuid

from pydantic import ValidationError
import structlog

from shared.contracts.bot_access import (
    QA_TEST_TELEGRAM_ID,
    TEST_IDENTITY_ENV_KEY,
    bot_admits,
    project_bot_audience,
)
from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.repository import RepositoryDTO
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import (
    AllocationFailureReason,
    DeployRunResult,
    EngineeringRunResult,
    QARunResult,
)
from shared.contracts.dto.server import ServerStatus
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.architect import ArchitectMessage
from shared.contracts.queues.deploy import DeployMessage, DeployOutcome, DeployTrigger
from shared.contracts.queues.engineering import EngineeringMessage
from shared.contracts.queues.po import POSystemEvent, to_flat_fields
from shared.contracts.queues.qa import QAMessage, QAOutcome
from shared.notifications import notify_admins_best_effort
from shared.queues import (
    ARCHITECT_QUEUE,
    DEPLOY_QUEUE,
    ENGINEERING_QUEUE,
    PO_INPUT_QUEUE,
    QA_QUEUE,
)
from shared.redis_client import RedisStreamClient

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient

from .. import startup
from .temporary_access import grant_temporary_access

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


def _qa_failure_limit() -> int:
    return startup.get_config().get_int("supervisor.qa_failure_max_fingerprint_attempts")


def _qa_fix_limit() -> int:
    return startup.get_config().get_int("supervisor.qa_max_fix_attempts")


def _resource_wait_timeout_minutes() -> int:
    return startup.get_config().get_int("supervisor.resource_wait_timeout_minutes")


def _resource_wait_metrics_freshness_seconds() -> int:
    return startup.get_config().get_int("supervisor.resource_wait_metrics_freshness_seconds")


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

        if await _park_task_waiting_resources(api_client, redis_client, task, log):
            continue

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


async def _park_task_waiting_resources(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    task,
    log: structlog.stdlib.BoundLogger,
) -> bool:
    """Park only waitable capacity failures; metrics failures remain technical failures."""
    runs = await api_client.list_runs(task_id=task.id, run_type=RunType.ENGINEERING.value)
    if not runs:
        return False
    run = runs[0]
    result = run.result
    if not result or not isinstance(result, EngineeringRunResult):
        return False
    reason = result.allocation_failure_reason
    if reason == AllocationFailureReason.IMPOSSIBLE_CAPACITY:
        await api_client.transition_task(task.id, TaskStatus.WAITING_HUMAN_REVIEW, "supervisor")
        if task.story_id:
            await api_client.transition_story(task.story_id, StoryStatus.WAITING_HUMAN_REVIEW)
        await _notify_admin_failure(
            task.id,
            str(task.project_id),
            "allocation request exceeds every managed server's capacity",
        )
        try:
            await _request_impossible_capacity_via_po(api_client, redis_client, task, log)
        except Exception:
            log.warning("impossible_capacity_request_failed", exc_info=True)
        log.warning("task_allocation_impossible")
        return True
    if reason not in {
        AllocationFailureReason.INSUFFICIENT_FREE_MEMORY,
        AllocationFailureReason.INSUFFICIENT_RESERVED_MEMORY,
    }:
        return False

    metadata = dict(task.failure_metadata or {})
    is_new_wait = "resource_wait_started_at" not in metadata
    metadata.setdefault("resource_wait_started_at", datetime.now(UTC).isoformat())
    metadata.update(
        {
            "allocation_required_ram_mb": result.allocation_required_ram_mb,
            "allocation_min_disk_mb": result.allocation_min_disk_mb,
            "allocation_failure_reason": reason.value,
        }
    )
    await api_client.update_task(task.id, {"failure_metadata": metadata})
    await api_client.transition_task(task.id, TaskStatus.WAITING_RESOURCES, "supervisor")
    log.info("task_waiting_resources", reason=reason.value)
    if is_new_wait:
        try:
            await _request_resources_via_po(api_client, redis_client, task, log)
        except Exception:
            log.warning("waiting_resources_request_failed", exc_info=True)
    return True


async def _request_impossible_capacity_via_po(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    task,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Ask PO to explain that the requested deployment cannot fit managed capacity."""
    project = await api_client.get_project(str(task.project_id))
    if project is None or not project.owner_id:
        return
    event = POSystemEvent(
        event="task_impossible_capacity",
        text=(
            "Engineering cannot place this project on any managed server. Tell the user that "
            "the request needs operator review."
        ),
        task_id=task.id,
        user_id=str(project.owner_id),
        project_id=str(task.project_id),
    )
    await redis_client.publish_flat(PO_INPUT_QUEUE, to_flat_fields(event))
    log.info("impossible_capacity_requested")


async def _request_resources_via_po(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    task,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Ask PO to tell the owner that engineering is waiting for capacity."""
    project = await api_client.get_project(str(task.project_id))
    if project is None or not project.owner_id:
        return
    event = POSystemEvent(
        event="task_waiting_resources",
        text=(
            "Engineering is waiting for server capacity. Tell the user that work will resume "
            "automatically when capacity becomes available."
        ),
        task_id=task.id,
        user_id=str(project.owner_id),
        project_id=str(task.project_id),
    )
    await redis_client.publish_flat(PO_INPUT_QUEUE, to_flat_fields(event))
    log.info("waiting_resources_requested")


async def supervise_waiting_resource_tasks(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """Resume capacity-parked tasks only after fresh metrics admit their request."""
    tasks = await api_client.get_tasks_by_status(TaskStatus.WAITING_RESOURCES)
    resumed = 0
    expired = 0
    for task in tasks:
        metadata = task.failure_metadata or {}
        started_at = _parse_datetime(
            metadata.get("resource_wait_started_at") or task.updated_at or task.created_at
        )
        age_minutes = (datetime.now(UTC) - started_at).total_seconds() / 60
        log = logger.bind(task_id=task.id, story_id=task.story_id)
        if age_minutes >= _resource_wait_timeout_minutes():
            await api_client.transition_task(task.id, TaskStatus.WAITING_HUMAN_REVIEW, "supervisor")
            await api_client.create_task_event(
                task.id,
                {
                    "event_type": "note",
                    "details": {
                        "reason": "resource_wait_timeout",
                        "age_minutes": round(age_minutes, 1),
                    },
                    "actor": "supervisor",
                },
            )
            await _notify_admin_failure(
                task.id,
                str(task.project_id),
                "resource wait timed out",
            )
            expired += 1
            continue
        if not await _resources_available(api_client, metadata):
            continue
        await _clear_failed_run_iteration(api_client, task)
        await api_client.transition_task(task.id, TaskStatus.BACKLOG, "supervisor")
        await api_client.transition_task(task.id, TaskStatus.TODO, "supervisor")
        try:
            await _notify_resources_resumed_via_po(api_client, redis_client, task)
        except Exception:
            log.warning("resources_resumed_request_failed", exc_info=True)
        resumed += 1
    return {"resumed": resumed, "expired": expired}


async def _clear_failed_run_iteration(api_client: SchedulerAPIClient, task) -> None:
    """Make the allocation-failed run ineligible for todo dispatch recovery.

    Capacity recovery is a fresh allocation attempt, but it does not consume a
    code-generation iteration. The dispatcher therefore needs the prior run's
    iteration stamp removed before the task returns to todo.
    """
    runs = await api_client.list_runs(task_id=task.id, run_type=RunType.ENGINEERING.value)
    for run in runs:
        if run.run_metadata.get("iteration") != task.current_iteration:
            continue
        await api_client.update_run(
            run.id,
            {"run_metadata": {**run.run_metadata, "iteration": None}},
        )
        return


async def _resources_available(api_client: SchedulerAPIClient, metadata: dict) -> bool:
    """Apply the allocator's conservative admission rule to fresh server metrics."""
    required_ram = metadata.get("allocation_required_ram_mb")
    min_disk = metadata.get("allocation_min_disk_mb")
    if not isinstance(required_ram, int) or not isinstance(min_disk, int):
        return False
    now = datetime.now(UTC)
    for server in await api_client.get_servers():
        if not server.is_managed or server.status not in {
            ServerStatus.ACTIVE,
            ServerStatus.READY,
            ServerStatus.IN_USE,
        }:
            continue
        if server.capacity_ram_mb < required_ram or server.capacity_disk_mb < min_disk:
            continue
        if not server.last_health_check:
            continue
        checked = server.last_health_check
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=UTC)
        age = (now - checked).total_seconds()
        if not 0 <= age <= _resource_wait_metrics_freshness_seconds():
            continue
        apps = await api_client.get_applications(server.handle)
        reserved = sum(
            app.reserved_ram_mb
            for app in apps
            if app.status not in {ApplicationStatus.NOT_DEPLOYED, ApplicationStatus.STOPPED}
        )
        if server.capacity_ram_mb >= max(reserved, server.used_ram_mb) + required_ram:
            return True
    return False


async def _notify_resources_resumed_via_po(
    api_client: SchedulerAPIClient, redis_client: RedisStreamClient, task
) -> None:
    project = await api_client.get_project(str(task.project_id))
    if project is None or not project.owner_id:
        return
    event = POSystemEvent(
        event="task_resources_resumed",
        text="Server capacity is available again. Tell the user that engineering has resumed.",
        task_id=task.id,
        user_id=str(project.owner_id),
        project_id=str(task.project_id),
    )
    await redis_client.publish_flat(PO_INPUT_QUEUE, to_flat_fields(event))


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
        return {"tested": 0, "retried": 0, "redispatched": 0, "waiting": 0, "failed": 0}

    tested = 0
    retried = 0
    redispatched = 0
    waiting = 0
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
                api_client, redis_client, story_id, project_id, run, run.result, log
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

        elif outcome == DeployOutcome.WAITING_FOR_USER_SECRET:
            await _handle_deploy_waiting_user_secret(
                api_client, redis_client, story_id, project_id, run, log
            )
            waiting += 1

        elif outcome in (
            DeployOutcome.GIVE_UP,
            DeployOutcome.ALLOCATION_MISSING,
            DeployOutcome.ENVIRONMENT_CONTRACT_INVALID,
            DeployOutcome.ENVIRONMENT_RESOLUTION_FAILED,
            DeployOutcome.HEAD_SHA_MISSING,
        ):
            await _handle_deploy_give_up(api_client, story_id, project_id, run, log)
            failed += 1

    return {
        "tested": tested,
        "retried": retried,
        "redispatched": redispatched,
        "waiting": waiting,
        "failed": failed,
    }


async def _handle_deploy_success_story(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    run,
    result: DeployRunResult,
    log: structlog.stdlib.BoundLogger,
) -> bool:
    """Deploy succeeded — transition story to TESTING and start the QA run.

    A private bot admits QA only through the deploy-time test slot, so the run
    is started from a temporary access grant instead of directly: the grant is
    recorded, the value is deployed on the same commit, and the sweep releases
    the QA message once that deploy confirms. Everything after that, including
    taking the access back, follows from the record.

    Returns True if the story was handed off to QA, False if QA's preconditions
    were not met (handled as a visible failure).
    """
    deployed_url = result.deployed_url
    application_id = result.application_id

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
    repo = await _resolve_qa_repository(api_client, project_id, log)
    if repo is None:
        await api_client.fail_story(story_id)
        await _notify_admin_failure(
            story_id,
            project_id,
            "deploy succeeded but the project's repository has no acceptance criteria — "
            "cannot run QA",
        )
        return False

    acceptance_criteria = repo.acceptance_criteria.strip()

    # The bot username is persisted on the repository when the user's token is
    # validated, so QA gets it even when the deploy smoke check could not resolve
    # it via getMe. The smoke value is the older source and stays as a fallback
    # for projects whose token was stored before it was persisted.
    bot_username = repo.bot_username or result.bot_username

    # The access the QA identity needs is decided before anything moves, so a
    # story that cannot be granted it fails visibly instead of reaching TESTING
    # with a run that can only be refused by the bot.
    head_sha = _deploy_run_head_sha(run)
    grant_needed = await _temporary_access_is_needed(api_client, project_id, result, log)
    if grant_needed is None:
        await api_client.fail_story(story_id)
        await _notify_admin_failure(
            story_id, project_id, "deploy succeeded but the project is gone — cannot run QA"
        )
        return False
    if grant_needed and not head_sha:
        log.error("deploy_success_head_sha_missing_for_access_grant", run_id=run.id)
        await api_client.fail_story(story_id)
        await _notify_admin_failure(
            story_id,
            project_id,
            "deploy succeeded but its commit is unknown — QA cannot be granted temporary access",
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
            "run_metadata": {"application_id": application_id},
        }
    )

    qa_message = QAMessage(
        story_id=story_id,
        project_id=project_id,
        user_id="",
        deployed_url=deployed_url,
        application_id=application_id,
        acceptance_criteria=acceptance_criteria,
        bot_username=bot_username,
        run_id=qa_run_id,
    )

    if grant_needed:
        grant = await grant_temporary_access(
            api_client,
            redis_client,
            project_id=project_id,
            env_key=TEST_IDENTITY_ENV_KEY,
            subject=str(QA_TEST_TELEGRAM_ID),
            head_sha=head_sha,
            qa_message=qa_message,
        )
        log.info(
            "deploy_supervisor_qa_handoff_awaiting_access",
            deployed_url=deployed_url,
            qa_run_id=qa_run_id,
            bot_username=bot_username,
            grant_id=grant.id,
        )
        return True

    await redis_client.publish_message(QA_QUEUE, qa_message)
    log.info(
        "deploy_supervisor_qa_handoff",
        deployed_url=deployed_url,
        qa_run_id=qa_run_id,
        bot_username=bot_username,
    )
    return True


async def _temporary_access_is_needed(
    api_client: SchedulerAPIClient,
    project_id: str,
    result: DeployRunResult,
    log: structlog.stdlib.BoundLogger,
) -> bool | None:
    """Whether this QA run has to borrow the deployed bot's test identity slot.

    Two deployments do not: one whose audience already admits the QA identity
    (a public bot, or a project that listed it), and one whose commit declares
    no test slot at all. The second is reported, because it means QA will be
    refused by a private bot and the deployed code is why.

    None means the audience could not be read at all, which the caller turns
    into a visible failure rather than a guess about who the bot admits.
    """
    if not result.test_identity_slot:
        log.warning("qa_handoff_without_test_identity_slot", project_id=project_id)
        return False

    project = await api_client.get_project(project_id)
    if project is None:
        log.error("qa_handoff_project_missing", project_id=project_id)
        return None

    audience = project_bot_audience(project.config)
    if bot_admits(audience=audience, test_identity="", telegram_id=QA_TEST_TELEGRAM_ID):
        log.info("qa_handoff_without_temporary_access", reason="audience_already_admits_qa")
        return False
    return True


async def _resolve_qa_repository(
    api_client: SchedulerAPIClient,
    project_id: str,
    log: structlog.stdlib.BoundLogger,
) -> RepositoryDTO | None:
    """Read the repository QA runs against, or None if it can't drive a QA run.

    Carries both the acceptance criteria and the bot username, so the QA handoff
    reads one record instead of two.
    """
    repo = await api_client.get_primary_repository(project_id)
    if repo is None:
        log.error("deploy_success_no_primary_repository")
        return None

    if not (repo.acceptance_criteria or "").strip():
        log.error("deploy_success_no_acceptance_criteria", repo_id=repo.id)
        return None
    return repo


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
    head_sha = _deploy_run_head_sha(run)
    if not head_sha:
        log.error("deploy_retry_head_sha_missing", run_id=run.id)
        await api_client.fail_story(story_id)
        await _notify_admin_failure(
            run.id, project_id, "deploy retry could not find original head_sha"
        )
        return False

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
            "run_metadata": {
                "triggered_by": "supervisor_retry",
                "attempt": attempts,
                "head_sha": head_sha,
            },
        }
    )

    deploy_msg = DeployMessage(
        task_id=new_run_id,
        project_id=project_id,
        user_id="",
        story_id=story_id,
        triggered_by=DeployTrigger.WEBHOOK,
        action="feature",
        head_sha=head_sha,
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


def _deploy_run_head_sha(run) -> str | None:
    """Read the exact commit a deploy run targeted, from its run_metadata."""
    run_metadata = getattr(run, "run_metadata", None) or {}
    return run_metadata.get("head_sha")


async def _handle_deploy_waiting_user_secret(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    run,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Deploy is blocked on a required user secret — park the story, ask the user once.

    The story moves DEPLOYING → WAITING_USER_SECRET (not FAILED). The request is
    emitted here, on entry to the wait, exactly once: the transition happens first,
    so the story leaves the DEPLOYING set this branch polls and cannot be asked
    again on a later tick. supervise_waiting_user_secret_stories only checks for the
    secret's arrival; it never re-sends the request.
    """
    missing = run.result.missing_user_secrets
    log.info(
        "deploy_waiting_user_secret",
        run_id=run.id,
        missing=[m.key for m in missing],
    )

    await api_client.wait_user_secret_story(story_id)

    try:
        await _request_user_secret_via_po(
            api_client, redis_client, story_id, project_id, missing, log
        )
    except Exception:
        # The story is already parked; a failed PO publish must not re-raise and
        # cause a second request next tick. It is a one-shot best-effort nudge.
        log.warning("waiting_user_secret_request_failed", story_id=story_id, exc_info=True)


async def _request_user_secret_via_po(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    missing,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Ask the project owner for the missing secrets, through PO, by key + description.

    Emits a POSystemEvent to po:input; PO composes the human message. The secret
    `consumers` never leave the resolver — only the key and its description reach
    the user.
    """
    project = await api_client.get_project(project_id)
    if project is None:
        log.warning("waiting_user_secret_no_project", project_id=project_id)
        return
    user_id = str(project.owner_id)
    if not user_id:
        log.warning("waiting_user_secret_no_owner", project_id=project_id)
        return

    secret_lines = "\n".join(f"- {m.key}: {m.description}" for m in missing)
    text = (
        "Deployment is paused because the project needs secret(s) only the user can "
        "provide:\n"
        f"{secret_lines}\n"
        "Ask the user for each value and save it. Deployment resumes automatically "
        "once every secret is saved."
    )
    event = POSystemEvent(
        event="story_waiting_user_secret",
        text=text,
        task_id=story_id,
        user_id=user_id,
        project_id=project_id,
    )
    await redis_client.publish_flat(PO_INPUT_QUEUE, to_flat_fields(event))
    log.info(
        "waiting_user_secret_requested",
        story_id=story_id,
        keys=[m.key for m in missing],
    )


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


async def _notify_admin_failure(entity_id: str, project_id: str, error: str) -> None:
    """Notify after a terminal failure has already been committed."""
    await notify_admins_best_effort(
        f"Supervisor failure for {entity_id} (project {project_id}):\n{error[:500]}",
        level="error",
        component="supervisor",
        run_id=entity_id,
        project_id=project_id,
    )


# ---------------------------------------------------------------------------
# WAITING_USER_SECRET supervision — re-deploy once the secret appears
# ---------------------------------------------------------------------------


async def supervise_waiting_user_secret_stories(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """Poll WAITING_USER_SECRET stories; re-deploy once every missing secret is saved.

    Reads the missing keys from the story's latest deploy run, checks the project's
    stored secret key names, and re-dispatches the deploy — the same way RETRY does
    — when all are present, moving the story back to DEPLOYING. A story whose set is
    still incomplete stays waiting: no state change, no repeated message to the user.

    Returns dict with 'redispatched' and 'failed' counts.
    """
    stories = await api_client.get_stories_by_status(StoryStatus.WAITING_USER_SECRET)
    if not stories:
        return {"redispatched": 0, "failed": 0}

    redispatched = 0
    failed = 0

    for story in stories:
        story_id = story.id
        project_id = str(story.project_id)
        log = logger.bind(story_id=story_id, project_id=project_id)

        try:
            run = await api_client.get_latest_run_by_story(story_id, run_type="deploy")
        except ValidationError as exc:
            await _fail_story_on_invalid_result(
                api_client, story_id, project_id, "deploy", exc, log
            )
            failed += 1
            continue
        # A run without a parseable result (QUEUED/RUNNING re-dispatch already in
        # flight, or superseded) means there is nothing to act on yet — keep waiting.
        if run is None or run.result is None:
            continue

        missing_keys = [m.key for m in run.result.missing_user_secrets]
        if not missing_keys:
            continue

        present = set(await api_client.list_project_secret_keys(project_id))
        if not set(missing_keys) <= present:
            log.info(
                "waiting_user_secret_incomplete",
                missing=[k for k in missing_keys if k not in present],
            )
            continue

        redeployed = await _redispatch_waiting_deploy(
            api_client, redis_client, story_id, project_id, run, log
        )
        if redeployed:
            redispatched += 1
        else:
            failed += 1

    return {"redispatched": redispatched, "failed": failed}


async def _redispatch_waiting_deploy(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    run,
    log: structlog.stdlib.BoundLogger,
) -> bool:
    """Every missing secret is saved — re-run deploy the same path RETRY uses.

    head_sha is resolved from the source run exactly as the RETRY path does; a
    missing head_sha is a typed failure (fail the story, notify admin), never a
    silent fallback to the default branch. The story is moved to DEPLOYING first so
    it leaves the WAITING set; if the publish then fails, next tick re-derives the
    wait from the old run rather than wedging on a queued run with no message.

    Returns True once re-dispatched, False if the story was failed instead.
    """
    head_sha = _deploy_run_head_sha(run)
    if not head_sha:
        log.error("waiting_user_secret_head_sha_missing", run_id=run.id)
        await api_client.fail_story(story_id)
        await _notify_admin_failure(
            run.id, project_id, "waiting deploy could not find original head_sha"
        )
        return False

    await api_client.transition_story(story_id, "deploy")

    new_run_id = f"deploy-secret-{uuid.uuid4().hex[:8]}"
    await api_client.create_run(
        {
            "id": new_run_id,
            "type": RunType.DEPLOY.value,
            "project_id": project_id,
            "story_id": story_id,
            "status": RunStatus.QUEUED.value,
            "run_metadata": {
                "triggered_by": "supervisor_user_secret",
                "head_sha": head_sha,
            },
        }
    )

    deploy_msg = DeployMessage(
        task_id=new_run_id,
        project_id=project_id,
        user_id="",
        story_id=story_id,
        triggered_by=DeployTrigger.WEBHOOK,
        action="feature",
        head_sha=head_sha,
    )
    await redis_client.publish_message(DEPLOY_QUEUE, deploy_msg)
    log.info("waiting_user_secret_redispatched", story_id=story_id, new_run_id=new_run_id)
    return True


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
    - BLOCKED / EXHAUSTED / ERROR → stop the application and wait for human review

    A story whose QA run still holds temporary access is not routed at all yet.
    Completing it would publish a successful outcome while the test identity is
    still admitted by the deployed bot, and a revoke that fails afterwards would
    have nothing left to report against.

    Returns dict with counts of actions taken.
    """
    stories = await api_client.get_stories_by_status(StoryStatus.TESTING)
    if not stories:
        return {"completed": 0, "redispatched": 0, "failed": 0, "waiting_for_access": 0}

    completed = 0
    redispatched = 0
    failed = 0
    waiting_for_access = 0

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

        grant = await api_client.get_live_temporary_access_grant_for_run(run.id)
        if grant is not None and grant.escalated_at is None:
            # The sweep is still working on the access. Once it either takes it
            # back or reports that it cannot, this story routes on what the QA
            # run says then — which for an unrevoked grant is a blocker.
            log.info(
                "qa_supervisor_waiting_for_access_revoke",
                run_id=run.id,
                grant_id=grant.id,
                grant_status=grant.status.value,
            )
            waiting_for_access += 1
            continue

        outcome = run.result.qa_outcome

        if outcome == QAOutcome.PASSED:
            await api_client.transition_story(story_id, "complete")
            log.info("qa_supervisor_completed", run_id=run.id)
            completed += 1

        elif outcome == QAOutcome.FAILED:
            dispatched = await _handle_qa_failed(
                api_client, redis_client, story_id, project_id, run.id, run.result, log
            )
            if dispatched:
                redispatched += 1
            elif dispatched is False:
                failed += 1

        elif outcome in (QAOutcome.BLOCKED, QAOutcome.EXHAUSTED, QAOutcome.ERROR):
            await _quarantine_unverified_application(
                api_client, redis_client, story_id, project_id, run, log
            )
            log.warning(
                "qa_supervisor_quarantined",
                run_id=run.id,
                outcome=outcome.value,
                application_id=run.run_metadata.get("application_id"),
            )
            failed += 1

    return {
        "completed": completed,
        "redispatched": redispatched,
        "failed": failed,
        "waiting_for_access": waiting_for_access,
    }


def _qa_quarantine_reason(result: QARunResult) -> dict:
    """Keep the terminal QA evidence with the story without reclassifying it."""
    reason = {"qa_outcome": result.qa_outcome.value}
    if result.blocker is not None:
        reason["blocker"] = result.blocker.model_dump(mode="json")
    if result.summary:
        reason["summary"] = result.summary
    if result.error:
        reason["error"] = result.error
    if result.state_changes:
        reason["state_changes"] = [
            change.model_dump(mode="json") for change in result.state_changes
        ]
    return reason


async def _quarantine_unverified_application(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    run,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Stop an unverified bot, retain its binding, and request a human decision."""
    application_id = run.run_metadata.get("application_id")
    if not isinstance(application_id, int):
        raise RuntimeError(f"QA run {run.id} has no application_id for quarantine")

    await api_client.stop_application(application_id)
    reason = _qa_quarantine_reason(run.result)
    await api_client.update_story(story_id, {"quarantine_reason": reason})
    await api_client.transition_story(story_id, "human-review")
    await _notify_quarantine_owner(api_client, redis_client, story_id, project_id, reason, log)


async def _notify_quarantine_owner(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    reason: dict,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Ask the project owner to decide what to do with a stopped bot."""
    project = await api_client.get_project(project_id)
    if project is None:
        log.warning("qa_quarantine_no_project", project_id=project_id)
        return

    outcome = reason["qa_outcome"]
    blocker = reason.get("blocker")
    if blocker:
        detail = f"{blocker['category']}: {blocker['received']}"
    else:
        detail = reason.get("summary") or reason.get("error") or outcome
    event = POSystemEvent(
        event="story_quarantined",
        text=(
            "QA could not confirm that the bot works. The bot has been stopped, "
            f"but its Telegram token remains assigned to this project. Reason: {detail}. "
            "Please decide whether to fix and redeploy it."
        ),
        task_id=story_id,
        user_id=str(project.owner_id),
        project_id=project_id,
    )
    await redis_client.publish_flat(PO_INPUT_QUEUE, to_flat_fields(event))


async def _handle_qa_failed(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    qa_run_id: str,
    result: QARunResult,
    log: structlog.stdlib.BoundLogger,
) -> bool | None:
    """Create a bounded, fingerprinted fix task for a confirmed QA defect.

    Returns True if a fix task was created, False if escalation is required,
    and None when an existing task was recovered or had already been handled.
    """
    summary = result.summary or "QA testing failed"
    failed_checks = result.failed_checks

    tasks = await api_client.get_tasks_by_story(story_id)
    prior_evidence = [item for task in tasks if (item := _qa_failure_metadata(task))]
    if any(item.get("qa_run_id") == qa_run_id for item in prior_evidence):
        # create_task commits before this transition. Retry the transition when
        # a transient error left the already-created fix task behind.
        await api_client.transition_story(story_id, "start")
        log.info("qa_supervisor_failure_transition_recovered", qa_run_id=qa_run_id)
        return None

    fingerprint = _qa_failure_fingerprint(summary, failed_checks)
    matching_failures = [item for item in prior_evidence if item.get("fingerprint") == fingerprint]
    attempt = len(matching_failures) + 1
    total_attempt = len(prior_evidence) + 1
    evidence = {
        "qa_run_id": qa_run_id,
        "fingerprint": fingerprint,
        "fingerprint_attempt": attempt,
        "fix_attempt": total_attempt,
        "summary": summary,
        "failed_checks": [check.model_dump(mode="json") for check in failed_checks],
    }

    if attempt > _qa_failure_limit() or total_attempt > _qa_fix_limit():
        await api_client.update_story(
            story_id,
            {"quarantine_reason": {"qa_outcome": QAOutcome.FAILED.value, "qa_failure": evidence}},
        )
        await api_client.transition_story(story_id, "human-review")
        exhausted_limit = _qa_failure_limit() if attempt > _qa_failure_limit() else _qa_fix_limit()
        await notify_admins_best_effort(
            f"QA failure {fingerprint} exhausted {exhausted_limit} fix attempts "
            f"for story {story_id}",
            level="warning",
            story_id=story_id,
            failure_fingerprint=fingerprint,
        )
        log.warning(
            "qa_supervisor_failure_escalated",
            fingerprint=fingerprint,
            fingerprint_attempt=attempt,
            fix_attempt=total_attempt,
            max_attempts=exhausted_limit,
        )
        return False

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
            "failure_metadata": {"qa_failure": evidence},
        }
    )

    # Transition story back to IN_PROGRESS for engineering
    await api_client.transition_story(story_id, "start")

    log.info(
        "qa_supervisor_fix_task_created",
        story_id=story_id,
        fingerprint=fingerprint,
        fingerprint_attempt=attempt,
        fix_attempt=total_attempt,
    )
    return True


def _qa_failure_metadata(task: object) -> dict | None:
    """Return the QA failure evidence recorded on a prior fix task."""
    metadata = getattr(task, "failure_metadata", None) or {}
    value = metadata.get("qa_failure")
    return value if isinstance(value, dict) else None


def _qa_failure_fingerprint(summary: str, failed_checks: list) -> str:
    """Build a stable signature for a QA failure's product evidence."""
    payload = {
        "failed_checks": [check.model_dump(mode="json") for check in failed_checks],
        "summary": summary,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).lower()
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]

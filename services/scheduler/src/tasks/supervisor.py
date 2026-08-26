"""Pipeline supervisor — detect stuck stories/tasks, retry or escalate."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
import hashlib
import json
from typing import TYPE_CHECKING
import uuid

from pydantic import ValidationError
import structlog

from shared.allocation_disposition import (
    PlacementPath,
    RefusalRouting,
    attempt_disposition,
    may_terminate_story,
    refusal_routing,
)
from shared.contracts.bot_access import (
    QA_TEST_TELEGRAM_ID,
    TEST_IDENTITY_ENV_KEY,
    bot_admits,
    project_bot_audience,
)
from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.project import (
    ProjectDTO,
    ProjectPredatesRunOwnership,
    require_initiating_run,
)
from shared.contracts.dto.qa_handoff import (
    QA_DISPATCHED_AT_KEY,
    QA_HANDOFF_KEY,
    QAHandoffPlan,
    TemporaryAccessRequest,
)
from shared.contracts.dto.repository import RepositoryDTO
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import (
    AllocationFailureReason,
    DeployRunResult,
    EngineeringRunResult,
    QARunResult,
)
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.dto.work_admission import PaidRunStartCommand, WorkAdmissionOutcome
from shared.contracts.queues.architect import ArchitectMessage
from shared.contracts.queues.deploy import (
    DeployAction,
    DeployMessage,
    DeployOutcome,
    DeployTrigger,
)
from shared.contracts.queues.engineering import EngineeringMessage
from shared.contracts.queues.po import POSystemEvent, to_flat_fields
from shared.contracts.queues.qa import QAMessage, QAOutcome
from shared.contracts.vocab import OwnerNotificationEvent
from shared.notifications import notify_admins_best_effort
from shared.queues import (
    ARCHITECT_QUEUE,
    DEPLOY_QUEUE,
    ENGINEERING_QUEUE,
    PO_INPUT_QUEUE,
    QA_QUEUE,
)
from shared.redis_client import RedisStreamClient
from shared.server_admission import (
    provisioning_failed_server_handles,
    server_admits_application,
)

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient

from .. import startup
from ._recipients import resolve_project_recipient
from .owner_notifications import deliver_owed_notification, owe_owner_notification
from .temporary_access import grant_temporary_access
from .worker_liveness import (
    WorkerAttemptState,
    attempt_state,
    fail_removed_attempt as _fail_removed_attempt,
    replay_terminal_attempt,
    request_stuck_attempt_stop,
    select_live_engineering_run,
    select_terminal_engineering_run,
)

logger = structlog.get_logger(__name__)

STORY_RETRY_KEY_PREFIX = "story:architect_retries:"
DEPLOY_RETRY_KEY_PREFIX = "deploy:retries:"

#: Where a deploy that carried an infrastructure wait forward started waiting.
#: Stored in `run_metadata` so the bound survives every re-dispatch.
INFRASTRUCTURE_WAIT_STARTED_KEY = "infrastructure_wait_started_at"

# A Run which failed before its EngineeringMessage reached the queue is terminal
# evidence for recovery, but never provider work. The reservation is released
# before this status update so terminal finalization cannot turn it into an
# unknown-cost hold.
DEPLOY_FIX_PUBLISH_FAILED_ERROR = "deploy-fix publish failed"

# The durable owner-notification record lives on the failed deploy Run, which
# remains available after the denied fix parks the story in human review.
ENGINEERING_BUDGET_DENIED_TEXT = (
    "Engineering cannot start the deploy fix because this project's engineering budget is "
    "currently exhausted. Tell the user that the work is waiting for their review."
)


class RefusedDeployAction(StrEnum):
    """What the deploy path did with one refused placement, for the tick counts.

    Three outcomes, because the shared table gives the refusal dispositions three
    behaviours. An escalation counted as a wait would be the same collapse in the
    reporting that the routing no longer allows.
    """

    REDISPATCHED = "redispatched"
    WAITING = "waiting"
    ESCALATED = "escalated"


#: The API exposes story transitions as action endpoints, not as status values:
#: `POST stories/{id}/human-review` is what moves a story into the human-review
#: queue. Posting `waiting_human_review` instead is a 404 — an escalation that
#: reaches nobody — so the action lives here once and every caller uses it.
STORY_HUMAN_REVIEW_ACTION = "human-review"


def _max_deploy_retries() -> int:
    return startup.get_config().get_int("deploy.max_deploy_retries")


def _max_deploy_fix_attempts() -> int:
    return startup.get_config().get_int("deploy.max_deploy_fix_attempts")


def _deploy_retry_ttl() -> int:
    return startup.get_config().get_int("deploy.deploy_retry_ttl")


def _story_stuck_threshold() -> int:
    return startup.get_config().get_int("supervisor.story_stuck_threshold_minutes")


def _max_architect_retries() -> int:
    return startup.get_config().get_int("supervisor.story_max_architect_retries")


def _story_retry_ttl() -> int:
    return startup.get_config().get_int("supervisor.story_retry_ttl")


def _qa_failure_limit() -> int:
    return startup.get_config().get_int("supervisor.qa_failure_max_fingerprint_attempts")


def _qa_fix_limit() -> int:
    return startup.get_config().get_int("supervisor.qa_max_fix_attempts")


def _qa_handoff_recovery_minutes() -> int:
    return startup.get_config().get_int("supervisor.qa_handoff_recovery_minutes")


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

        # Retry: the story's owner is reached through its project, so the
        # lifecycle events this retry produces still have somewhere to go.
        recipient = await resolve_project_recipient(
            api_client, project_id, event="story_stuck_retry", story_id=story_id
        )
        arch_msg = ArchitectMessage(
            story_id=story_id,
            project_id=project_id,
            telegram_chat_id=recipient.telegram_chat_id,
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
                    await api_client.transition_story(story_id, STORY_HUMAN_REVIEW_ACTION)
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
    """Route a failed engineering run by what its allocation refusal actually was.

    The classification comes from `shared.allocation_disposition`, the one place
    that decides what an allocation refusal may do, and the behaviour comes from
    the same table's engineering row; this function keeps no reason list and no
    behaviour list of its own, so it cannot drift from the deploy path.
    `product_failure` is True because a FAILED engineering run is otherwise the
    code's failure — and the shared rule is what says an allocation refusal
    outranks that.

    Returns True when this function has routed the task, False when the caller's
    own failure routing applies.
    """
    runs = await api_client.list_runs(task_id=task.id, run_type=RunType.ENGINEERING.value)
    if not runs:
        return False
    run = runs[0]
    result = run.result
    if not result or not isinstance(result, EngineeringRunResult):
        return False
    reason = result.allocation_failure_reason
    disposition = attempt_disposition(reason, product_failure=True)
    routing = refusal_routing(PlacementPath.ENGINEERING, disposition)
    if routing is RefusalRouting.HUMAN_REVIEW_WITH_OWNER_NOTICE:
        # The same seam as the story-level endings, and for the same reason:
        # `_escalate_task_to_human_review` below commits the parent story's
        # human-review transition, after which no loop scans it, so the notice
        # is written on this engineering run before the transition rather than
        # published behind the `except Exception: log.warning` that used to
        # swallow it. The task id is kept on the record because this ending is
        # about the task, and that is what PO answers about.
        owed = await owe_owner_notification(
            api_client,
            run,
            event=OwnerNotificationEvent.TASK_IMPOSSIBLE_CAPACITY,
            text=IMPOSSIBLE_CAPACITY_TASK_TEXT,
            story_id=task.story_id,
            project_id=str(task.project_id),
            terminal_status=StoryStatus.WAITING_HUMAN_REVIEW,
            task_id=task.id,
            log=log,
        )
        await _escalate_task_to_human_review(
            api_client,
            task,
            "allocation request exceeds every managed server's capacity",
        )
        await deliver_owed_notification(api_client, redis_client, run.id, owed, log)
        log.warning("task_allocation_impossible")
        return True
    if routing is RefusalRouting.HUMAN_REVIEW_PLATFORM_ALERT:
        # The allocator could not evaluate the fleet at all. Parking would never
        # end — the wait's own re-check needs the metrics that are missing — and
        # retrying the code charges the platform's blind spot to the user's
        # iteration budget for a run that will be refused at the same point. So
        # it stops here, with operators told and the owner left out of it.
        await _escalate_task_to_human_review(
            api_client,
            task,
            f"placement could not be evaluated: {reason.value}",
        )
        log.warning("task_allocation_unevaluable", reason=reason.value)
        return True
    if routing is not RefusalRouting.PARK_WAITING_RESOURCES:
        # CALLER_FAILURE_ROUTING / NO_REFUSAL: no allocation refusal happened,
        # so this is the code's own failure and the caller retries it.
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
        # An unfinished host build waits on the same path, but the owner must not
        # be told the platform ran out of capacity when it did not.
        request_via_po = (
            _request_infrastructure_wait_via_po
            if reason is AllocationFailureReason.SERVER_NOT_PROVISIONED
            else _request_resources_via_po
        )
        try:
            await request_via_po(api_client, redis_client, task, log)
        except Exception:
            log.warning("waiting_resources_request_failed", exc_info=True)
    return True


async def _escalate_task_to_human_review(
    api_client: SchedulerAPIClient,
    task,
    detail: str,
) -> None:
    """Hand a task the platform cannot place to the human-review queue.

    The story moves through the `human-review` action, which is the endpoint the
    API exposes for that queue; the status value is not a route, and posting it
    as one reached nothing.
    """
    await api_client.transition_task(task.id, TaskStatus.WAITING_HUMAN_REVIEW, "supervisor")
    if task.story_id:
        await api_client.transition_story(task.story_id, STORY_HUMAN_REVIEW_ACTION)
    await _notify_admin_failure(task.id, str(task.project_id), detail)


#: What the owner is told when engineering cannot be placed anywhere at all.
#: Terminal, unlike the two waits below it: nothing frees up that makes this
#: request fit, so the task and its story stop for an operator instead of
#: waiting, and the message says that rather than promising a resumption.
IMPOSSIBLE_CAPACITY_TASK_TEXT = (
    "Engineering cannot place this project on any managed server. Tell the user that "
    "the request needs operator review."
)


async def _request_resources_via_po(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    task,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Ask PO to tell the owner that engineering is waiting for capacity."""
    recipient = await resolve_project_recipient(
        api_client, str(task.project_id), event="task_waiting_resources", story_id=task.story_id
    )
    if not recipient.is_addressable:
        return
    event = POSystemEvent(
        event=OwnerNotificationEvent.TASK_WAITING_RESOURCES,
        text=(
            "Engineering is waiting for server capacity. Tell the user that work will resume "
            "automatically when capacity becomes available."
        ),
        task_id=task.id,
        story_id=task.story_id or "",
        telegram_chat_id=recipient.telegram_chat_id,
        owner_user_id=recipient.owner_user_id,
        project_id=str(task.project_id),
    )
    await redis_client.publish_flat(PO_INPUT_QUEUE, to_flat_fields(event))
    log.info("waiting_resources_requested")


async def _request_infrastructure_wait_via_po(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    task,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Ask PO to tell the owner the target machine is still being prepared.

    This is deliberately not the capacity message: nothing is full and the user's
    project is not defective — the host it would run on has not finished (or has
    failed) its software provisioning, which operators and the provisioner resolve.
    """
    recipient = await resolve_project_recipient(
        api_client,
        str(task.project_id),
        event=OwnerNotificationEvent.TASK_WAITING_INFRASTRUCTURE,
        story_id=task.story_id,
    )
    if not recipient.is_addressable:
        return
    event = POSystemEvent(
        event="task_waiting_infrastructure",
        text=(
            "Engineering is waiting for a server whose setup is still being finished on our "
            "side. Tell the user this is our infrastructure, not a problem with their project, "
            "and that work will resume automatically once the server is ready."
        ),
        task_id=task.id,
        story_id=task.story_id or "",
        telegram_chat_id=recipient.telegram_chat_id,
        owner_user_id=recipient.owner_user_id,
        project_id=str(task.project_id),
    )
    await redis_client.publish_flat(PO_INPUT_QUEUE, to_flat_fields(event))
    log.info("waiting_infrastructure_requested")


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
    """Apply the allocator's conservative admission rule to fresh server metrics.

    Target admissibility comes from ``shared.server_admission``, the same predicate
    the allocator applies, so a parked task never wakes up towards a server that
    ``_find_suitable_server`` would then refuse — an unprovisioned or
    provisioning-failed host is not "resources becoming available".
    """
    required_ram = metadata.get("allocation_required_ram_mb")
    min_disk = metadata.get("allocation_min_disk_mb")
    if not isinstance(required_ram, int) or not isinstance(min_disk, int):
        return False
    return await _admissible_target_exists(
        api_client, required_ram_mb=required_ram, min_disk_mb=min_disk
    )


async def _admissible_target_exists(
    api_client: SchedulerAPIClient, *, required_ram_mb: int, min_disk_mb: int
) -> bool:
    """Whether any server could take this request right now.

    Both waits ask this: the engineering task parked in `waiting_resources` and
    the deploy run parked on `WAITING_INFRASTRUCTURE`. Admissibility itself comes
    from `shared.server_admission`, the predicate the allocator applies, so no
    wait can end towards a target the allocator would refuse.
    """
    now = datetime.now(UTC)
    provisioning_failed_handles = provisioning_failed_server_handles(
        await api_client.list_active_incidents()
    )
    for server in await api_client.get_servers():
        if not server_admits_application(server, provisioning_failed_handles):
            continue
        if server.capacity_ram_mb < required_ram_mb or server.capacity_disk_mb < min_disk_mb:
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
        if server.capacity_ram_mb >= max(reserved, server.used_ram_mb) + required_ram_mb:
            return True
    return False


async def _notify_resources_resumed_via_po(
    api_client: SchedulerAPIClient, redis_client: RedisStreamClient, task
) -> None:
    recipient = await resolve_project_recipient(
        api_client, str(task.project_id), event="task_resources_resumed", story_id=task.story_id
    )
    if not recipient.is_addressable:
        return
    event = POSystemEvent(
        event=OwnerNotificationEvent.TASK_RESOURCES_RESUMED,
        text="Server capacity is available again. Tell the user that engineering has resumed.",
        task_id=task.id,
        story_id=task.story_id or "",
        telegram_chat_id=recipient.telegram_chat_id,
        owner_user_id=recipient.owner_user_id,
        project_id=str(task.project_id),
    )
    await redis_client.publish_flat(PO_INPUT_QUEUE, to_flat_fields(event))


async def supervise_stuck_tasks(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """Reconcile active leases, Docker endings and a finite turn deadline."""
    tasks = await api_client.get_tasks_by_status(TaskStatus.IN_DEV)
    timed_out = 0
    working = 0
    stopping = 0
    now = datetime.now(UTC)

    for task in tasks:
        run = await select_live_engineering_run(api_client, task.id)
        if run is None:
            terminal_run = await select_terminal_engineering_run(api_client, task.id)
            if terminal_run is not None:
                await replay_terminal_attempt(api_client, task.id, terminal_run, "supervisor")
            continue
        state, worker_id = await attempt_state(redis_client, run, now)

        if state is WorkerAttemptState.RUNNING:
            working += 1
            continue
        if state in {WorkerAttemptState.IDLE, WorkerAttemptState.UNKNOWN}:
            continue
        if state is WorkerAttemptState.REMOVED:
            await _fail_removed_attempt(api_client, task, run)
            timed_out += 1
            continue
        # No worker was ever recorded for this attempt, so there is no
        # container teardown to await.  Its absolute request deadline is the
        # terminal evidence; continuing to publish an empty stop intent would
        # otherwise leave the run open forever.
        if state is WorkerAttemptState.TIMED_OUT and worker_id is None:
            await _fail_removed_attempt(api_client, task, run)
            timed_out += 1
            continue
        await request_stuck_attempt_stop(api_client, redis_client, task, run, state, worker_id, now)
        stopping += 1

    return {"timed_out": timed_out, "working": working, "stopping": stopping}


async def supervise_deploying_stories(  # noqa: PLR0912
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """Poll DEPLOYING stories and route based on deploy run outcome.

    Reads run.result.deploy_outcome set by the deploy worker:
    - SUCCESS → story TESTING, publish QAMessage
    - SMOKE_FAILURE / CODE_FIX → story IN_PROGRESS, redispatch to engineering
    - RETRY → increment retry counter, re-publish DeployMessage or FAILED
    - WAITING_INFRASTRUCTURE → routed by `shared.allocation_disposition`, which
      gives each refusal disposition its own behaviour: a wait that resumes, an
      escalation to the human-review queue, or an operator alert. Never failed:
      the deploy never ran, because the platform had no server to run it on.
    - GIVE_UP → story FAILED, notify admins

    Returns dict with counts of actions taken.
    """
    stories = await api_client.get_stories_by_status(StoryStatus.DEPLOYING)
    if not stories:
        return {
            "tested": 0,
            "retried": 0,
            "redispatched": 0,
            "waiting": 0,
            "escalated": 0,
            "failed": 0,
        }

    tested = 0
    retried = 0
    redispatched = 0
    waiting = 0
    failed = 0
    refused: dict[RefusedDeployAction, int] = dict.fromkeys(RefusedDeployAction, 0)
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

        elif outcome in (DeployOutcome.RETRY, DeployOutcome.CANCELLED):
            # A cancelled deploy did not fail and did not deploy: something took
            # the project away from it — the fence a temporary-access revoke
            # takes, or another deploy holding the lock. The story still needs
            # its commit deployed, so it goes round again under the same bound
            # that stops a failing deploy from looping.
            if outcome is DeployOutcome.CANCELLED:
                log.info("deploy_supervisor_redeploy_after_cancel", run_id=run.id)
            was_retried = await _handle_deploy_retry(
                api_client, redis_client, redis, story_id, project_id, run, log
            )
            if was_retried:
                retried += 1
            else:
                failed += 1

        elif outcome is DeployOutcome.WAITING_INFRASTRUCTURE:
            action = await _route_refused_deploy(
                api_client, redis_client, story_id, project_id, run, run.result, log
            )
            # Each action names the counter it advances, so a behaviour the
            # routing distinguishes cannot be merged back together in the counts.
            refused[action] += 1

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
        "redispatched": redispatched + refused[RefusedDeployAction.REDISPATCHED],
        "waiting": waiting + refused[RefusedDeployAction.WAITING],
        "escalated": refused[RefusedDeployAction.ESCALATED],
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
    # The project is read once here and used twice: it says who the bot admits,
    # and it carries the run that initiated this work, which the QA message has
    # to hand on so the QA executor is owned by the same run as the developer
    # workers that produced the code under test.
    project = await api_client.get_project(project_id)
    if project is None:
        log.error("qa_handoff_project_missing", project_id=project_id)
        await api_client.fail_story(story_id)
        await _notify_admin_failure(
            story_id, project_id, "deploy succeeded but the project is gone — cannot run QA"
        )
        return False

    # A project that predates run ownership names no run, so its QA executor
    # could not be attributed once it dies. Fail the story rather than create an
    # unownable worker — the same refusal the API gives an admin.
    try:
        initiating_run_id = require_initiating_run(project)
    except ProjectPredatesRunOwnership as exc:
        log.error(
            "qa_handoff_project_has_no_initiating_run", project_id=project_id, reason=str(exc)
        )
        await api_client.fail_story(story_id)
        await _notify_admin_failure(
            story_id,
            project_id,
            "deploy succeeded but the project names no initiating run — cannot run QA",
        )
        return False

    grant_needed = _temporary_access_is_needed(project, result, log)
    if grant_needed and not head_sha:
        log.error("deploy_success_head_sha_missing_for_access_grant", run_id=run.id)
        await api_client.fail_story(story_id)
        await _notify_admin_failure(
            story_id,
            project_id,
            "deploy succeeded but its commit is unknown — QA cannot be granted temporary access",
        )
        return False

    # The QA run is created before the story leaves DEPLOYING, and it carries the
    # whole plan. Order matters in both directions: a crash before this leaves
    # the story where the deploy supervisor still sees it and this runs again,
    # and a crash after it leaves a run that says what was supposed to happen.
    # Its id is derived from the deploy run, so the retry lands on the same run
    # instead of creating a second one for the same deploy.
    qa_run_id = _qa_run_id_for_deploy(run.id)
    qa_recipient = await resolve_project_recipient(
        api_client, project_id, event="qa_dispatch", story_id=story_id
    )
    qa_message = QAMessage(
        story_id=story_id,
        project_id=project_id,
        initiating_run_id=initiating_run_id,
        telegram_chat_id=qa_recipient.telegram_chat_id,
        deployed_url=deployed_url,
        application_id=application_id,
        acceptance_criteria=acceptance_criteria,
        bot_username=bot_username,
        run_id=qa_run_id,
    )
    plan = QAHandoffPlan(
        qa_message=qa_message,
        access=TemporaryAccessRequest(
            env_key=TEST_IDENTITY_ENV_KEY,
            subject=str(QA_TEST_TELEGRAM_ID),
            head_sha=head_sha,
        )
        if grant_needed
        else None,
    )
    started = await api_client.start_paid_run(
        PaidRunStartCommand(
            id=qa_run_id,
            type=RunType.QA,
            project_id=uuid.UUID(project_id),
            story_id=story_id,
            run_metadata={
                "application_id": application_id,
                QA_HANDOFF_KEY: plan.model_dump(mode="json"),
            },
        )
    )
    if started.admission.outcome is not WorkAdmissionOutcome.ADMITTED:
        log.info(
            "qa_handoff_count_admission_refused",
            qa_run_id=qa_run_id,
            reason=(
                started.admission.reason.value if started.admission.reason is not None else None
            ),
        )
        if started.admission.message:
            owed = await owe_owner_notification(
                api_client,
                run,
                event=OwnerNotificationEvent.STORY_QUARANTINED,
                text=started.admission.message,
                story_id=story_id,
                project_id=project_id,
                terminal_status=StoryStatus.WAITING_HUMAN_REVIEW,
                log=log,
            )
            await api_client.transition_story(story_id, STORY_HUMAN_REVIEW_ACTION)
            await deliver_owed_notification(api_client, redis_client, run.id, owed, log)
        return False
    await api_client.transition_story(story_id, "test")

    await _execute_qa_handoff(api_client, redis_client, qa_run_id, plan, log)
    return True


def _qa_run_id_for_deploy(deploy_run_id: str) -> str:
    """One QA run per deploy run, named so a repeat of the handoff finds it."""
    return f"qa-{deploy_run_id}"[:255]


async def _execute_qa_handoff(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    qa_run_id: str,
    plan: QAHandoffPlan,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Do what the stored plan says, from whichever tick gets there.

    Either the access is recorded — after which the temporary-access sweep owns
    the run and releases it once the value is confirmed deployed — or the message
    goes straight to QA. Both are safe to repeat: the grant id is derived from
    the run, so a second attempt returns the first record, and the publish is
    stamped on the run so it is not repeated once it landed.
    """
    if plan.access is not None:
        grant = await grant_temporary_access(
            api_client,
            redis_client,
            project_id=plan.qa_message.project_id,
            env_key=plan.access.env_key,
            subject=plan.access.subject,
            head_sha=plan.access.head_sha,
            qa_message=plan.qa_message,
        )
        log.info(
            "deploy_supervisor_qa_handoff_awaiting_access",
            deployed_url=plan.qa_message.deployed_url,
            qa_run_id=qa_run_id,
            bot_username=plan.qa_message.bot_username,
            grant_id=grant.id,
        )
        return

    await redis_client.publish_message(QA_QUEUE, plan.qa_message)
    await api_client.update_run(
        qa_run_id,
        {"run_metadata": {QA_DISPATCHED_AT_KEY: datetime.now(UTC).isoformat()}},
    )
    log.info(
        "deploy_supervisor_qa_handoff",
        deployed_url=plan.qa_message.deployed_url,
        qa_run_id=qa_run_id,
        bot_username=plan.qa_message.bot_username,
    )


def _temporary_access_is_needed(
    project: ProjectDTO,
    result: DeployRunResult,
    log: structlog.stdlib.BoundLogger,
) -> bool:
    """Whether this QA run has to borrow the deployed bot's test identity slot.

    Two deployments do not: one whose audience already admits the QA identity
    (a public bot, or a project that listed it), and one whose commit declares
    no test slot at all. The second is reported, because it means QA will be
    refused by a private bot and the deployed code is why.

    The project is read by the caller, which needs it anyway and fails the story
    visibly when it is gone, so this decides the question rather than also
    answering "the audience could not be read at all".
    """
    if not result.test_identity_slot:
        log.warning("qa_handoff_without_test_identity_slot", project_id=str(project.id))
        return False

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
    # A fix is another attempt inside the run that initiated the work, so the
    # message carries the project's run: the worker it spawns belongs to the
    # same run as the one whose deploy failed.
    project = await api_client.get_project(project_id)
    if project is None:
        log.error("deploy_fix_project_missing", project_id=project_id)
        await api_client.fail_story(story_id)
        await _notify_admin_failure(run.id, project_id, "deploy fix needs a project that is gone")
        return False

    # Same refusal as the QA handoff: no initiating run, no ownable worker, so
    # the fix attempt is not started at all rather than started unattributable.
    try:
        initiating_run_id = require_initiating_run(project)
    except ProjectPredatesRunOwnership as exc:
        log.error(
            "deploy_fix_project_has_no_initiating_run", project_id=project_id, reason=str(exc)
        )
        await api_client.fail_story(story_id)
        await _notify_admin_failure(
            run.id, project_id, "deploy fix needs a project that names its initiating run"
        )
        return False

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

    error_details = result.error_details or "unknown deploy error"
    fix_task_id = f"eng-deploy-fix-{run.id}-{attempt + 1}"
    try:
        started = await api_client.start_paid_run(
            PaidRunStartCommand(
                id=fix_task_id,
                type=RunType.ENGINEERING,
                project_id=project.id,
                task_id=fix_task_id,
                story_id=story_id,
                run_metadata={"deploy_fix_attempt": attempt + 1},
            )
        )
    except Exception:
        log.exception("deploy_fix_paid_start_failed", run_id=fix_task_id)
        return False
    if started.admission.outcome is not WorkAdmissionOutcome.ADMITTED:
        budget = started.engineering_budget
        reason = {
            "reason": (
                "engineering_budget_denied"
                if budget is not None
                else (started.admission.reason.value if started.admission.reason else "denied")
            ),
            "attempt_id": fix_task_id,
        }
        if budget is not None:
            reason.update(
                known_spend_microusd=budget.known_spend_microusd,
                active_held_microusd=budget.active_held_microusd,
                available_microusd=budget.available_microusd,
            )
        log.info("deploy_fix_admission_refused", **reason)
        await api_client.update_story(story_id, {"quarantine_reason": reason})
        owed = await owe_owner_notification(
            api_client,
            run,
            event=OwnerNotificationEvent.STORY_QUARANTINED,
            text=started.admission.message or ENGINEERING_BUDGET_DENIED_TEXT,
            story_id=story_id,
            project_id=project_id,
            terminal_status=StoryStatus.WAITING_HUMAN_REVIEW,
            log=log,
        )
        await api_client.transition_story(story_id, STORY_HUMAN_REVIEW_ACTION)
        await deliver_owed_notification(api_client, redis_client, run.id, owed, log)
        return False

    # Every action from admission through publication is pre-handoff. It must
    # release an admitted hold if it fails, because publication is the first
    # point provider work could have started.
    run_created = False
    try:
        # Transition story back to IN_PROGRESS only for an admitted handoff.
        await api_client.transition_story(story_id, "start")
        run_created = True
        fix_recipient = await resolve_project_recipient(
            api_client, project_id, event="deploy_code_fix", story_id=story_id
        )
        fix_msg = EngineeringMessage(
            task_id=fix_task_id,
            project_id=project_id,
            initiating_run_id=initiating_run_id,
            telegram_chat_id=fix_recipient.telegram_chat_id,
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
    except Exception:
        log.exception("deploy_fix_pre_handoff_failed", fix_task_id=fix_task_id)
        if run_created:
            await api_client.abort_paid_run_pre_handoff(
                fix_task_id, DEPLOY_FIX_PUBLISH_FAILED_ERROR
            )
        return False
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

    retry_recipient = await resolve_project_recipient(
        api_client, project_id, event="deploy_retry", story_id=story_id
    )
    deploy_msg = DeployMessage(
        task_id=new_run_id,
        project_id=project_id,
        telegram_chat_id=retry_recipient.telegram_chat_id,
        unaddressed_reason=retry_recipient.unaddressed_reason,
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


async def _route_refused_deploy(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    run,
    result: DeployRunResult,
    log: structlog.stdlib.BoundLogger,
) -> RefusedDeployAction:
    """Give the refusal the one behaviour the shared table owes its disposition.

    The classification is read from the run result rather than re-derived, and
    the behaviour comes from `shared.allocation_disposition` — the same table the
    engineering path consults — so this branch can neither treat a refusal as a
    product failure nor answer two dispositions the same way. Collapsing them is
    what left a request no server could ever fit polling forever with nobody
    told. The contract already refuses a `WAITING_INFRASTRUCTURE` result without
    its reason and budget, so both are present here.
    """
    reason = result.allocation_failure_reason
    disposition = attempt_disposition(reason, product_failure=True)
    routing = refusal_routing(PlacementPath.DEPLOY, disposition)
    log = log.bind(reason=reason.value, disposition=disposition.value, routing=routing.value)

    if routing is RefusalRouting.WAIT_FOR_ADMISSIBLE_TARGET:
        return await _handle_deploy_infrastructure_wait(
            api_client, redis_client, story_id, project_id, run, result, log
        )
    if routing is RefusalRouting.HUMAN_REVIEW_WITH_OWNER_NOTICE:
        await _escalate_refused_deploy(
            api_client,
            redis_client,
            story_id,
            project_id,
            run,
            result,
            tell_owner=True,
            detail=(
                f"deploy needs {result.allocation_required_ram_mb} MB RAM and "
                f"{result.allocation_min_disk_mb} MB disk, which exceeds every managed server"
            ),
            log=log,
        )
        return RefusedDeployAction.ESCALATED
    if routing is RefusalRouting.HUMAN_REVIEW_PLATFORM_ALERT:
        await _escalate_refused_deploy(
            api_client,
            redis_client,
            story_id,
            project_id,
            run,
            result,
            tell_owner=False,
            detail=f"deploy placement could not be evaluated: {reason.value}",
            log=log,
        )
        return RefusedDeployAction.ESCALATED

    # CALLER_FAILURE_ROUTING / NO_REFUSAL cannot be reached from an allocation
    # refusal while the shared table classifies every reason as infrastructure —
    # `may_terminate_story` says the same thing from the other side. If that ever
    # changes, a human hears about it: waiting would hide it and failing the
    # story would charge the platform's own mistake to the user's project.
    log.error(
        "deploy_refusal_misclassified",
        run_id=run.id,
        may_terminate_story=may_terminate_story(disposition),
    )
    await _escalate_refused_deploy(
        api_client,
        redis_client,
        story_id,
        project_id,
        run,
        result,
        tell_owner=False,
        detail=f"deploy refusal {reason.value} classified as {disposition.value}",
        log=log,
    )
    return RefusedDeployAction.ESCALATED


async def _escalate_refused_deploy(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    run,
    result: DeployRunResult,
    *,
    tell_owner: bool,
    detail: str,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Hand a refusal no wait can resolve to the human-review queue that exists.

    This is the same queue a quarantined QA story reaches, entered the same way:
    the reason is recorded on the story first, then the `human-review` action
    moves it. It is deliberately not `fail_story` — an infrastructure refusal is
    never evidence that the user's project is broken — and deliberately not
    another wait, because the condition it would wait for cannot change on its
    own.
    """
    await api_client.update_story(
        story_id,
        {
            "quarantine_reason": {
                "deploy_outcome": DeployOutcome.WAITING_INFRASTRUCTURE.value,
                "allocation_failure_reason": result.allocation_failure_reason.value,
                "allocation_required_ram_mb": result.allocation_required_ram_mb,
                "allocation_min_disk_mb": result.allocation_min_disk_mb,
                "detail": detail,
            }
        },
    )
    # Owed before the transition for the same reason the QA paths owe theirs:
    # this line takes the story out of DEPLOYING, and nothing scans it
    # afterwards. A refusal nobody is told about was previously one swallowed
    # exception away — the publish used to sit behind `except Exception: log`.
    owed = None
    if tell_owner:
        owed = await owe_owner_notification(
            api_client,
            run,
            event=OwnerNotificationEvent.STORY_IMPOSSIBLE_CAPACITY,
            text=IMPOSSIBLE_CAPACITY_TEXT,
            story_id=story_id,
            project_id=project_id,
            terminal_status=StoryStatus.WAITING_HUMAN_REVIEW,
            log=log,
        )
    await api_client.transition_story(story_id, STORY_HUMAN_REVIEW_ACTION)
    await _notify_admin_failure(run.id, project_id, detail)
    if owed is not None:
        await deliver_owed_notification(api_client, redis_client, run.id, owed, log)
    log.warning("deploy_refusal_escalated", run_id=run.id, detail=detail, told_owner=tell_owner)


#: What the owner is told when their deploy needs an operator rather than room.
#: Deliberately not the capacity-wait message: nothing will free up that makes
#: this request fit, and the project is not at fault, so the owner is told what
#: is actually happening instead of being left to watch a wait that never ends.
IMPOSSIBLE_CAPACITY_TEXT = (
    "Deploying this project needs more capacity than any managed server can provide. "
    "Tell the user that our operators have been asked to review it, and that this is "
    "our infrastructure, not a problem with their project."
)


def _infrastructure_wait_started_at(run) -> datetime:
    """When this story started waiting for infrastructure, across re-dispatches.

    Each re-dispatch carries the stamp forward, so the bound measures how long
    the user's deploy has actually been waiting rather than restarting every time
    the wait briefly resumed and was refused again.
    """
    run_metadata = getattr(run, "run_metadata", None) or {}
    stamp = run_metadata.get(INFRASTRUCTURE_WAIT_STARTED_KEY)
    return _parse_datetime(stamp) if stamp else _parse_datetime(run.created_at)


async def _handle_deploy_infrastructure_wait(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    run,
    result: DeployRunResult,
    log: structlog.stdlib.BoundLogger,
) -> RefusedDeployAction:
    """A deploy that found no admissible server waits, and never fails the story.

    The wait is bounded by `supervisor.resource_wait_timeout_minutes`, the same
    bound the engineering path's wait carries: the platform may keep a user's
    work waiting on its own infrastructure only so long before somebody has to
    look. An unbounded wait is how a stuck story stays invisible.

    The bound is checked before admissibility, exactly as the engineering path
    checks the task's age before `_resources_available`, because a wait can also
    fail to end while targets keep appearing. Resuming asks whether *any* server
    could take the request, while a project already bound to a host is refused by
    *that* host: a fleet with one healthy server and one broken one the project
    sits on would otherwise re-dispatch, be refused, and re-dispatch again
    forever. Escalating on elapsed time bounds that cycle too — the same clock,
    carried across re-dispatches, ends both shapes of a wait that is not working.
    """
    waiting_since = _infrastructure_wait_started_at(run)
    waited_minutes = (datetime.now(UTC) - waiting_since).total_seconds() / 60

    if waited_minutes >= _resource_wait_timeout_minutes():
        await _escalate_refused_deploy(
            api_client,
            redis_client,
            story_id,
            project_id,
            run,
            result,
            tell_owner=False,
            detail=(
                f"deploy waited {round(waited_minutes)} minutes for an admissible server "
                f"({result.allocation_failure_reason.value})"
            ),
            log=log,
        )
        return RefusedDeployAction.ESCALATED

    if not await _admissible_target_exists(
        api_client,
        required_ram_mb=result.allocation_required_ram_mb,
        min_disk_mb=result.allocation_min_disk_mb,
    ):
        log.info(
            "deploy_waiting_infrastructure",
            run_id=run.id,
            waited_minutes=round(waited_minutes, 1),
        )
        return RefusedDeployAction.WAITING

    head_sha = _deploy_run_head_sha(run)
    if not head_sha:
        # No wait can supply a commit this run never recorded, so waiting for one
        # is the silent hang again. The story is not failed — a deploy run
        # without a head_sha is this platform's defect, not the project's — it
        # goes to a human.
        log.error("deploy_infrastructure_wait_head_sha_missing", run_id=run.id)
        await _escalate_refused_deploy(
            api_client,
            redis_client,
            story_id,
            project_id,
            run,
            result,
            tell_owner=False,
            detail="deploy run has no head_sha to resume the infrastructure wait with",
            log=log,
        )
        return RefusedDeployAction.ESCALATED

    new_run_id = f"deploy-infra-{uuid.uuid4().hex[:8]}"
    await api_client.create_run(
        {
            "id": new_run_id,
            "type": RunType.DEPLOY.value,
            "project_id": project_id,
            "story_id": story_id,
            "status": RunStatus.QUEUED.value,
            "run_metadata": {
                "triggered_by": "supervisor_infrastructure_wait",
                "head_sha": head_sha,
                INFRASTRUCTURE_WAIT_STARTED_KEY: waiting_since.isoformat(),
            },
        }
    )
    recipient = await resolve_project_recipient(
        api_client, project_id, event="deploy_after_infrastructure_wait", story_id=story_id
    )
    await redis_client.publish_message(
        DEPLOY_QUEUE,
        DeployMessage(
            task_id=new_run_id,
            project_id=project_id,
            telegram_chat_id=recipient.telegram_chat_id,
            unaddressed_reason=recipient.unaddressed_reason,
            story_id=story_id,
            triggered_by=DeployTrigger.WEBHOOK,
            action=DeployAction.FEATURE,
            head_sha=head_sha,
        ),
    )
    log.info("deploy_infrastructure_wait_redispatched", run_id=run.id, new_run_id=new_run_id)
    return RefusedDeployAction.REDISPATCHED


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
    recipient = await resolve_project_recipient(
        api_client, project_id, event="story_waiting_user_secret", story_id=story_id
    )
    if not recipient.is_addressable:
        log.warning("waiting_user_secret_unaddressable", project_id=project_id)
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
        event=OwnerNotificationEvent.STORY_WAITING_USER_SECRET,
        text=text,
        task_id=story_id,
        story_id=story_id,
        telegram_chat_id=recipient.telegram_chat_id,
        owner_user_id=recipient.owner_user_id,
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

    secret_recipient = await resolve_project_recipient(
        api_client, project_id, event="deploy_after_user_secret", story_id=story_id
    )
    deploy_msg = DeployMessage(
        task_id=new_run_id,
        project_id=project_id,
        telegram_chat_id=secret_recipient.telegram_chat_id,
        unaddressed_reason=secret_recipient.unaddressed_reason,
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
    - PASSED → story COMPLETED and the owner told, with the deployment's address
    - FAILED → create fix task, story IN_PROGRESS, redispatch to engineering
    - BLOCKED / EXHAUSTED / ERROR → stop the application and wait for human review

    The temporary access the run borrowed is deliberately not consulted. Handing
    the test identity back is the sweep's work and it runs on its own schedule:
    it keeps revoking, keeps reading the running service, and calls an
    administrator when it gives up. Making the product wait for that meant a
    story that deploy, smoke and QA had all passed stayed unfinished for exactly
    as long as a revoke kept being retried, and the user heard nothing. A test
    identity left behind is a cleanup incident with its own owner, not a verdict
    on the product, so it is reported there instead of held against the story.

    Returns dict with counts of actions taken.
    """
    stories = await api_client.get_stories_by_status(StoryStatus.TESTING)
    if not stories:
        return {"completed": 0, "redispatched": 0, "failed": 0, "recovered": 0}

    completed = 0
    redispatched = 0
    failed = 0
    recovered = 0

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

        if run.status is RunStatus.QUEUED:
            # A queued QA run in a TESTING story is either about to be picked up
            # or is the remains of a handoff that died before it finished. The
            # plan stored on the run is what tells the two apart and what lets
            # this tick finish the work the dead process started.
            if await _recover_qa_handoff(api_client, redis_client, run, log):
                recovered += 1
            continue

        if run.status is RunStatus.RUNNING:
            continue

        # A terminal QA run always carries a result (validation enforces it);
        # None here only means a superseded/non-terminal run — skip it.
        if run.result is None:
            log.info("qa_run_superseded_skip", run_id=run.id, run_status=run.status.value)
            continue

        outcome = run.result.qa_outcome

        if outcome == QAOutcome.PASSED:
            # Owed before the transition, delivered after it: the story leaves
            # TESTING here and this loop never sees it again, so a message that
            # only existed as a publish attempt would be lost with the attempt.
            owed = await owe_owner_notification(
                api_client,
                run,
                event=OwnerNotificationEvent.STORY_COMPLETED,
                text=_story_completed_text(run),
                story_id=story_id,
                project_id=project_id,
                terminal_status=StoryStatus.COMPLETED,
                log=log,
            )
            await api_client.transition_story(story_id, "complete")
            log.info("qa_supervisor_completed", run_id=run.id)
            await deliver_owed_notification(api_client, redis_client, run.id, owed, log)
            completed += 1

        elif outcome == QAOutcome.FAILED:
            # Only a typed failed check is product evidence. A malformed or
            # contradictory failed result is not permission to ask engineering
            # to change customer code, so it takes the ordinary unverified QA
            # route instead.
            if run.result.blocker is not None or not run.result.failed_checks:
                await _quarantine_unverified_application(
                    api_client, redis_client, story_id, project_id, run, log
                )
                failed += 1
            else:
                dispatched = await _handle_qa_failed(
                    api_client, redis_client, story_id, project_id, run, log
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
        "recovered": recovered,
    }


def _story_completed_text(run) -> str:
    """What the owner is told about a product QA passed.

    The address comes from the handoff the QA run carries, which is the one
    deployment QA was pointed at: what the user is given is what was verified,
    not whatever the project happens to be running now. PO writes the words.

    Nothing here waits for the borrowed test identity to be handed back. That is
    the sweep's business, and it is finished — or escalated to an administrator —
    on its own schedule.
    """
    qa_message = QAHandoffPlan.model_validate(run.run_metadata[QA_HANDOFF_KEY]).qa_message
    address = qa_message.deployed_url
    if qa_message.bot_username:
        address = f"{address} (Telegram bot @{qa_message.bot_username})"
    return (
        "The story is finished: it is deployed and QA passed. Tell the user the good "
        f"news and give them the address: {address}"
    )


async def _recover_qa_handoff(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    run,
    log: structlog.stdlib.BoundLogger,
) -> bool:
    """Finish a QA handoff whose process died before it did.

    Everything that decides the handoff is on the run, so this needs no memory of
    the tick that planned it. What it must not do is repeat work that landed: a
    plan that wanted access is left alone once any grant exists for the run,
    because from that moment the temporary-access sweep owns it, and a plan that
    only had to publish is left alone once the publish is stamped.

    The age bound keeps this off a handoff that is merely in progress — a run
    created seconds ago is being worked on, not abandoned.

    Returns True if this tick took the handoff over.
    """
    plan_data = run.run_metadata.get(QA_HANDOFF_KEY)
    if plan_data is None:
        # A run from before the plan was recorded, or one created by something
        # other than the deploy handoff. Nothing here can be reconstructed.
        return False
    if run.run_metadata.get(QA_DISPATCHED_AT_KEY):
        return False

    age_minutes = (datetime.now(UTC) - _parse_datetime(run.created_at)).total_seconds() / 60
    if age_minutes < _qa_handoff_recovery_minutes():
        return False

    plan = QAHandoffPlan.model_validate(plan_data)
    if plan.access is not None and await api_client.temporary_access_grant_exists_for_run(run.id):
        return False

    log.warning(
        "qa_handoff_recovered",
        run_id=run.id,
        age_minutes=round(age_minutes, 1),
        needs_access=plan.access is not None,
    )
    await _execute_qa_handoff(api_client, redis_client, run.id, plan, log)
    return True


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
    if result.telegram_probe_evidence:
        reason["telegram_probe_evidence"] = [
            evidence.model_dump(mode="json") for evidence in result.telegram_probe_evidence
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
    owed = await owe_owner_notification(
        api_client,
        run,
        event=OwnerNotificationEvent.STORY_QUARANTINED,
        text=_quarantine_text(reason),
        story_id=story_id,
        project_id=project_id,
        terminal_status=StoryStatus.WAITING_HUMAN_REVIEW,
        log=log,
    )
    await api_client.transition_story(story_id, STORY_HUMAN_REVIEW_ACTION)
    await deliver_owed_notification(api_client, redis_client, run.id, owed, log)


def _quarantine_text(reason: dict) -> str:
    """Ask the project owner to decide what to do with a stopped bot."""
    outcome = reason["qa_outcome"]
    blocker = reason.get("blocker")
    if blocker:
        detail = f"{blocker['category']}: {blocker['received']}"
    else:
        detail = reason.get("summary") or reason.get("error") or outcome
    return (
        "QA could not confirm that the bot works. The bot has been stopped, "
        f"but its Telegram token remains assigned to this project. Reason: {detail}. "
        "Please decide whether to fix and redeploy it."
    )


async def _handle_qa_failed(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story_id: str,
    project_id: str,
    run,
    log: structlog.stdlib.BoundLogger,
) -> bool | None:
    """Create a bounded, fingerprinted fix task for a confirmed QA defect.

    Returns True if a fix task was created, False if escalation is required,
    and None when an existing task was recovered or had already been handled.
    """
    qa_run_id = run.id
    result = run.result
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
        exhausted_limit = _qa_failure_limit() if attempt > _qa_failure_limit() else _qa_fix_limit()
        await api_client.update_story(
            story_id,
            {"quarantine_reason": {"qa_outcome": QAOutcome.FAILED.value, "qa_failure": evidence}},
        )
        # The owner is told here, not only the administrators. This transition
        # ends the story for them exactly as a quarantine does — their product
        # stops moving until a human looks at it — and an ending they are not
        # told about is the silence this seam exists to remove. It goes through
        # the same record for the same reason: the story leaves TESTING on the
        # next line and nothing scans it afterwards.
        owed = await owe_owner_notification(
            api_client,
            run,
            event=OwnerNotificationEvent.STORY_QUARANTINED,
            text=_fix_attempts_exhausted_text(summary, exhausted_limit),
            story_id=story_id,
            project_id=project_id,
            terminal_status=StoryStatus.WAITING_HUMAN_REVIEW,
            log=log,
        )
        await api_client.transition_story(story_id, STORY_HUMAN_REVIEW_ACTION)
        await deliver_owed_notification(api_client, redis_client, run.id, owed, log)
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


def _fix_attempts_exhausted_text(summary: str, exhausted_limit: int) -> str:
    """What the owner is told when QA kept failing and the fixes ran out.

    Deliberately the same event PO already routes for a quarantine: from the
    owner's side this *is* the quarantine case — the product is stopped and a
    human has to decide — and inventing a second event name would only mean PO
    dropping it as unknown.
    """
    return (
        f"QA kept finding the same problem after {exhausted_limit} attempts to fix it, "
        "so work on this story has stopped and a specialist has been asked to look at it. "
        f"The last thing QA reported: {summary}"
    )


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

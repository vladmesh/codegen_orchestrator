"""Story, task, worker, and capacity liveness supervision."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from shared.allocation_disposition import (
    PlacementPath,
    RefusalRouting,
    attempt_disposition,
    refusal_routing,
)
from shared.contracts.dto.run import RunType
from shared.contracts.dto.run_result import (
    AllocationFailureReason,
    EngineeringRunResult,
)
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.architect import ArchitectMessage
from shared.contracts.queues.po import POSystemEvent, to_flat_fields
from shared.contracts.vocab import OwnerNotificationEvent
from shared.queues import (
    ARCHITECT_QUEUE,
    PO_INPUT_QUEUE,
)
from shared.redis_client import RedisStreamClient

if TYPE_CHECKING:
    from ...clients.api import SchedulerAPIClient

from ... import startup
from .._recipients import resolve_project_recipient
from ..owner_notifications import (
    deliver_owed_notification,
    owe_owner_notification,
)
from ..worker_liveness import (
    WorkerAttemptState,
    attempt_state,
    fail_removed_attempt as _fail_removed_attempt,
    replay_terminal_attempt,
    request_stuck_attempt_stop,
    select_live_engineering_run,
    select_terminal_engineering_run,
)
from .common import (
    STORY_HUMAN_REVIEW_ACTION,
    _admissible_target_exists,
    _notify_admin_failure,
    _parse_datetime,
    _resource_wait_timeout_minutes,
)

logger = structlog.get_logger(__name__)

STORY_RETRY_KEY_PREFIX = "story:architect_retries:"


def _story_stuck_threshold() -> int:
    return startup.get_config().get_int("supervisor.story_stuck_threshold_minutes")


def _max_architect_retries() -> int:
    return startup.get_config().get_int("supervisor.story_max_architect_retries")


def _story_retry_ttl() -> int:
    return startup.get_config().get_int("supervisor.story_retry_ttl")


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

"""Task Dispatcher — dispatches todo tasks, completes stories, supervises pipeline.

Responsibilities:
A) Ask the admission point about every todo task, publish the message for each
   attempt it admits, and act on the typed answer for each one it does not.
B) Find stories where all tasks are done → complete story + trigger deploy.
C) Supervise pipeline: detect stuck states, retry or fail-fast.

Runs as a periodic scheduler job (every 30s).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from shared.contracts.dto.engineering_budget_policy import EngineeringBudgetAdmissionOutcome
from shared.contracts.dto.engineering_dispatch import (
    EngineeringDispatchCommand,
    EngineeringDispatchOutcome,
    EngineeringDispatchRead,
    EngineeringDispatchRefusal,
    EngineeringDispatchRepair,
)
from shared.contracts.dto.run import RunDTO
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskDTO, TaskStatus, TaskType
from shared.contracts.queues.engineering import EngineeringMessage
from shared.contracts.vocab import ActionType, OwnerNotificationEvent
from shared.queues import ENGINEERING_QUEUE
from shared.redis import RedisStreamClient

from ._recipients import resolve_project_recipient
from .owner_notifications import (
    deliver_owed_notification,
    owe_owner_notification,
    supervise_owed_owner_notifications,
)
from .pr_poller import poll_ci_failures, poll_merged_prs
from .scaffold_trigger import trigger_scaffolds
from .story_completion import (
    _cleanup_story_worker,
    _parse_owner_repo,
    _trigger_next_story,
    complete_stories,
)
from .supervisor import (
    supervise_deploying_stories,
    supervise_failed_tasks,
    supervise_stuck_stories,
    supervise_stuck_tasks,
    supervise_testing_stories,
    supervise_waiting_resource_tasks,
    supervise_waiting_user_secret_stories,
)
from .temporary_access import supervise_temporary_access
from .worker_liveness import terminal_task_statuses

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient

__all__ = [
    "_build_cumulative_context",
    "_cleanup_story_worker",
    "_parse_owner_repo",
    "_trigger_next_story",
    "complete_stories",
    "dispatch_todo_tasks",
    "poll_merged_prs",
    "supervise_temporary_access",
    "task_dispatcher_loop",
]

from .. import startup

logger = structlog.get_logger(__name__)

# This is used only before calling the queue; a publication failure is never proof
# that the message did not land and must not be routed through the abort command.
PRE_HANDOFF_PREPARATION_FAILED_ERROR = "dispatch handoff preparation failed"

#: Repairs that leave the task in in_dev with work behind it, so this tick counts
#: it the way it counts a fresh dispatch.
_DISPATCH_COMPLETING = (
    EngineeringDispatchRepair.RECOVER_OWN_ATTEMPT,
    EngineeringDispatchRepair.REPLAY_FINISHED_RUN,
)


def _dispatch_interval() -> int:
    return startup.get_config().get_int("scheduler.dispatch_interval_seconds")


def _build_cumulative_context(sibling_events: list) -> str:
    """Build a context summary from completed sibling task events."""
    lines = []
    for event in sibling_events:
        if event.event_type != "iteration_end":
            continue
        details = event.details or {}
        summary = details.get("summary", "")
        commit = details.get("commit_sha", "")
        if summary:
            entry = f"- {summary}"
            if commit:
                entry += f" (commit: {commit})"
            lines.append(entry)
    if not lines:
        return ""
    return "## Context from completed tasks\n" + "\n".join(lines) + "\n\n"


async def _enriched_description(api_client: SchedulerAPIClient, task: TaskDTO) -> str:
    """The task's description with its story's finished work in front of it.

    Message building, never admission: this decides what the worker is told, and
    it runs only once the admission point has already admitted the dispatch. The
    sibling read here answers "what has been done" — the admission point does its
    own sibling read, on locked rows, to answer "may anything be done at all".
    """
    description = task.description or ""
    if not task.story_id:
        return description
    events = []
    for sibling in await api_client.get_tasks_by_story(task.story_id):
        if sibling.id != task.id and sibling.status == TaskStatus.DONE:
            events.extend(await api_client.get_task_events(sibling.id))
    context = _build_cumulative_context(events)
    return context + description if context else description


async def _recover_dispatched_task(
    api_client: SchedulerAPIClient,
    task_id: str,
    run: RunDTO,
    log: structlog.BoundLogger,
) -> None:
    """Replay a finished run's outcome onto a task the transition never left todo.

    in_dev is the only way out of todo, so the task goes there first and the
    outcome is applied on top — the result handler could not apply it while the
    task was still in todo, and without the replay the task would sit in in_dev
    with nothing working on it.

    The run is always finished by the time this is called: the admission point
    names this repair only for a run no longer in flight.
    """
    await api_client.transition_task(task_id, TaskStatus.IN_DEV, "dispatcher")
    for status in terminal_task_statuses(run):
        await api_client.transition_task(task_id, status, "dispatcher")
    log.info("task_outcome_replayed", run_id=run.id, run_status=run.status.value)


async def _execute_repair(
    api_client: SchedulerAPIClient,
    task: TaskDTO,
    decision: EngineeringDispatchRead,
    log: structlog.BoundLogger,
) -> bool:
    """Carry out the repair the admission point decided this task still owes.

    The fence that produced it is server-side and returns a decision; the
    transitions it implies are executed here, so nothing is committed by a
    question. Returns whether the task ends this tick with work behind it.
    """
    if decision.repair is EngineeringDispatchRepair.REPLAY_FINISHED_RUN:
        run = await api_client.get_run(decision.run_id)
        await _recover_dispatched_task(api_client, task.id, run, log)
        return True
    log.info(
        (
            "task_transition_recovered"
            if decision.repair is EngineeringDispatchRepair.RECOVER_OWN_ATTEMPT
            else "task_dispatch_blocked_by_live_attempt"
        ),
        run_id=decision.run_id,
        iteration=task.current_iteration,
    )
    await api_client.transition_task(task.id, TaskStatus.IN_DEV, "dispatcher")
    return decision.repair in _DISPATCH_COMPLETING


async def _handle_refusal(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    task: TaskDTO,
    decision: EngineeringDispatchRead,
    log: structlog.BoundLogger,
) -> None:
    """Act on a refusal: only a refusal that already counted an attempt routes.

    `paid_work` is present exactly when the paid gate decided, and that is the
    line: a refusal from an earlier condition is a state this tick simply cannot
    dispatch in and a later tick may, while a paid denial has spent the attempt
    and hands the story and the task to a human instead of retrying.
    """
    if decision.paid_work is None:
        log.info("task_dispatch_refused", reason=decision.reason.value)
        return
    admission = decision.paid_work.admission
    if task.story_id and admission.message:
        source_run = await api_client.get_run(decision.initiating_run_id)
        owed = await owe_owner_notification(
            api_client,
            source_run,
            event=OwnerNotificationEvent.STORY_QUARANTINED,
            text=admission.message,
            story_id=task.story_id,
            project_id=str(task.project_id),
            terminal_status=StoryStatus.WAITING_HUMAN_REVIEW,
            task_id=task.id,
            log=log,
        )
        await api_client.transition_story(task.story_id, "human-review")
        await deliver_owed_notification(api_client, redis_client, source_run.id, owed, log)
    budget = decision.paid_work.engineering_budget
    await api_client.transition_task(task.id, TaskStatus.IN_DEV, "dispatcher")
    if budget is not None and budget.outcome is EngineeringBudgetAdmissionOutcome.DENIED:
        details = {
            "reason": EngineeringDispatchRefusal.ENGINEERING_BUDGET_DENIED.value,
            "attempt_id": budget.attempt_id,
            "known_spend_microusd": budget.known_spend_microusd,
            "active_held_microusd": budget.active_held_microusd,
            "available_microusd": budget.available_microusd,
        }
    else:
        details = {"reason": decision.reason.value, "attempt_id": decision.run_id}
    await api_client.transition_task(
        task.id, TaskStatus.WAITING_HUMAN_REVIEW, "dispatcher", details=details
    )
    log.info(
        "task_dispatch_count_admission_refused",
        run_id=decision.run_id,
        task_id=task.id,
        reason=decision.reason.value,
    )


async def _publish_admitted_dispatch(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    task: TaskDTO,
    decision: EngineeringDispatchRead,
    log: structlog.BoundLogger,
) -> bool:
    """Hand an admitted attempt to the engineering queue and leave todo.

    The attempt already exists and already holds its budget: the admission point
    created it. What is left is the message and the transition, and only a
    failure proven to precede the queue call releases the reservation. A lost
    publish response keeps the queued Run and its hold, so normal live-attempt
    recovery owns it on the next tick.
    """
    run_id = decision.run_id
    try:
        action = ActionType.FEATURE if task.type is TaskType.REFACTOR else ActionType(task.type)
        recipient = await resolve_project_recipient(
            api_client, str(task.project_id), event="task_dispatch", story_id=task.story_id or ""
        )
        eng_msg = EngineeringMessage(
            task_id=run_id,
            project_id=str(task.project_id),
            initiating_run_id=decision.initiating_run_id,
            telegram_chat_id=recipient.telegram_chat_id,
            action=action,
            description=await _enriched_description(api_client, task),
            skip_deploy=True,  # Deploy handled at story level
            planning_task_id=task.id,
            story_id=task.story_id,
            branch=f"story/{task.story_id}" if task.story_id else None,
        )
    except Exception:
        # This block contains only work proven to precede any queue call.  Do
        # not include publication: a lost publish response has an unknown outcome.
        log.exception("task_dispatch_pre_handoff_preparation_failed", run_id=run_id)
        await api_client.abort_paid_run_pre_handoff(run_id, PRE_HANDOFF_PREPARATION_FAILED_ERROR)
        return False
    try:
        await redis_client.publish_message(ENGINEERING_QUEUE, eng_msg)
    except Exception:
        # Publication may have reached Redis before its response was lost.  Keep
        # the queued Run and active hold so normal unfinished-run recovery owns it.
        log.exception("task_dispatch_publish_outcome_unknown", run_id=run_id)
        return False
    if not await _transition_to_in_dev(api_client, task.id, run_id, log):
        return False
    log.info("task_dispatched", run_id=run_id)
    return True


async def _transition_to_in_dev(
    api_client: SchedulerAPIClient,
    task_id: str,
    run_id: str,
    log: structlog.BoundLogger,
) -> bool:
    """Move a task to in_dev, retrying once.

    The message is already out, so the run is live and the task must not stay in
    todo. If both attempts fail, the admission point's live-attempt repair
    finishes the transition on the next tick.
    """
    try:
        await api_client.transition_task(task_id, TaskStatus.IN_DEV, "dispatcher")
    except Exception:
        log.warning("task_transition_retry", run_id=run_id, exc_info=True)
        try:
            await api_client.transition_task(task_id, TaskStatus.IN_DEV, "dispatcher")
        except Exception:
            log.exception("task_transition_failed", run_id=run_id)
            return False
    return True


async def dispatch_todo_tasks(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> int:
    """Ask the admission point about every todo task and act on its answer.

    This function selects the candidates and executes decisions; it holds no
    admission condition of its own. Whether a task may be dispatched — the
    internal project, the scaffold, the workspace, the blocker, the story, the
    prior attempt, the budget and the slot — is one question answered server-side
    on locked rows by `admit_engineering_dispatch`.

    Returns the number of tasks dispatched.
    """
    dispatched = 0

    for task in await api_client.get_tasks_by_status(TaskStatus.TODO):
        log = logger.bind(task_id=task.id, story_id=task.story_id)
        try:
            decision = await api_client.admit_engineering_dispatch(
                EngineeringDispatchCommand(task_id=task.id)
            )
        except Exception:
            # Nothing was decided, so nothing was counted and nothing is owed.
            log.exception("task_dispatch_admission_failed")
            continue

        if decision.outcome is EngineeringDispatchOutcome.REFUSED:
            await _handle_refusal(api_client, redis_client, task, decision, log)
        elif decision.outcome is EngineeringDispatchOutcome.REPAIR:
            if await _execute_repair(api_client, task, decision, log):
                dispatched += 1
        elif await _publish_admitted_dispatch(api_client, redis_client, task, decision, log):
            dispatched += 1

    return dispatched


async def task_dispatcher_loop() -> None:
    """Periodic loop: dispatch tasks + complete stories every 30s."""
    from ..clients.api import api_client

    redis_client = RedisStreamClient()
    await redis_client.connect()

    logger.info("task_dispatcher_started", interval=_dispatch_interval())

    try:
        while True:
            try:
                scaffolds = await trigger_scaffolds(api_client, redis_client)
                dispatched = await dispatch_todo_tasks(api_client, redis_client)
                completed = await complete_stories(api_client, redis_client)
                merged = await poll_merged_prs(api_client, redis_client)
                await poll_ci_failures(api_client)

                # Supervisor checks
                stuck_stories = await supervise_stuck_stories(api_client, redis_client)
                stuck_tasks = await supervise_stuck_tasks(api_client, redis_client)
                failed_tasks = await supervise_failed_tasks(api_client, redis_client)
                waiting_resources = await supervise_waiting_resource_tasks(api_client, redis_client)
                deploying = await supervise_deploying_stories(api_client, redis_client)
                waiting_secret = await supervise_waiting_user_secret_stories(
                    api_client, redis_client
                )
                # Messages a committed terminal transition still owes are
                # re-attempted before the routing that owes new ones. Ordered
                # this way round, a record written by this tick's routing gets
                # exactly the one in-tick attempt routing makes; the other way
                # round the sweep would immediately spend a second attempt of
                # the bound on it, in the same second.
                owner_notifications = await supervise_owed_owner_notifications(
                    api_client, redis_client
                )
                # Stories are routed on their QA runs before the access sweep
                # runs, and that order is the delivery guarantee: a product QA
                # has passed is handed to its owner on the tick that reads the
                # verdict, and the cleanup of the identity it borrowed happens
                # afterwards. Sweeping first would let a cleanup that ran out of
                # attempts during a gap in this loop write its incident on the QA
                # run before the story had been routed, turning a passed product
                # into a quarantine over a leftover test user.
                testing = await supervise_testing_stories(api_client, redis_client)
                temporary_access = await supervise_temporary_access(api_client, redis_client)

                # Always log the cycle summary for observability
                logger.info(
                    "dispatcher_cycle",
                    tasks_dispatched=dispatched,
                    stories_completed=completed,
                    scaffolds_triggered=scaffolds,
                    prs_merged=merged,
                )
                supervisor_active = (
                    stuck_stories.get("retried", 0)
                    + stuck_stories.get("failed", 0)
                    + stuck_tasks.get("timed_out", 0)
                    + failed_tasks.get("retried", 0)
                    + failed_tasks.get("escalated", 0)
                    + waiting_resources.get("resumed", 0)
                    + waiting_resources.get("expired", 0)
                    + deploying.get("tested", 0)
                    + deploying.get("retried", 0)
                    + deploying.get("redispatched", 0)
                    + deploying.get("waiting", 0)
                    + deploying.get("escalated", 0)
                    + deploying.get("failed", 0)
                    + waiting_secret.get("redispatched", 0)
                    + waiting_secret.get("failed", 0)
                    + testing.get("completed", 0)
                    + testing.get("redispatched", 0)
                    + testing.get("failed", 0)
                    + temporary_access.get("dispatched", 0)
                    + temporary_access.get("released", 0)
                    + temporary_access.get("revoked", 0)
                    + temporary_access.get("revoke_failed", 0)
                    + temporary_access.get("escalated", 0)
                    + owner_notifications["delivered"]
                    + owner_notifications["retrying"]
                    + owner_notifications["exhausted"]
                    + owner_notifications["unaddressable"]
                    + owner_notifications["voided"]
                )
                if supervisor_active:
                    logger.info(
                        "supervisor_cycle",
                        stories_retried=stuck_stories.get("retried", 0),
                        stories_failed=stuck_stories.get("failed", 0),
                        tasks_timed_out=stuck_tasks.get("timed_out", 0),
                        tasks_retried=failed_tasks.get("retried", 0),
                        tasks_escalated=failed_tasks.get("escalated", 0),
                        deploy_tested=deploying.get("tested", 0),
                        deploy_retried=deploying.get("retried", 0),
                        deploy_redispatched=deploying.get("redispatched", 0),
                        deploy_waiting_user_secret=deploying.get("waiting", 0),
                        deploy_escalated=deploying.get("escalated", 0),
                        deploy_failed=deploying.get("failed", 0),
                        user_secret_redispatched=waiting_secret.get("redispatched", 0),
                        user_secret_failed=waiting_secret.get("failed", 0),
                        qa_completed=testing.get("completed", 0),
                        qa_redispatched=testing.get("redispatched", 0),
                        qa_failed=testing.get("failed", 0),
                        temporary_access_dispatched=temporary_access.get("dispatched", 0),
                        temporary_access_released=temporary_access.get("released", 0),
                        temporary_access_revoked=temporary_access.get("revoked", 0),
                        temporary_access_expired=temporary_access.get("expired", 0),
                        # Still being chased vs. given up on and handed to a human.
                        temporary_access_revoke_failed=temporary_access.get("revoke_failed", 0),
                        temporary_access_escalated=temporary_access.get("escalated", 0),
                        # Owner notifications recovered from a committed
                        # terminal transition whose publish did not land. Still
                        # being chased vs. given up on and handed to a human vs.
                        # refused because the owner has no chat to write to.
                        owner_notify_recovered=owner_notifications["delivered"],
                        owner_notify_retrying=owner_notifications["retrying"],
                        owner_notify_exhausted=owner_notifications["exhausted"],
                        owner_notify_unaddressable=owner_notifications["unaddressable"],
                        # A record whose transition never committed: nothing was
                        # sent, nothing was spent, and the ending is owed again
                        # if routing does reach it.
                        owner_notify_voided=owner_notifications["voided"],
                    )
            except Exception:
                logger.exception("dispatcher_cycle_error")
            await asyncio.sleep(_dispatch_interval())
    finally:
        await redis_client.close()
        logger.info("task_dispatcher_stopped")

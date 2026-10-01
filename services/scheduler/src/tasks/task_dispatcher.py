"""Task Dispatcher — admits and dispatches engineering work.

The dispatcher owns one responsibility: ask the durable admission point about
TODO tasks and hand admitted work to the engineering queue. Scaffold triggering,
story completion, lifecycle supervision, PR/CI routing and QA routing run in
independent scheduler-pipeline loops with their own failure boundaries.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx
import structlog

from shared.contracts.dto.engineering_budget_policy import EngineeringBudgetAdmissionOutcome
from shared.contracts.dto.engineering_dispatch import (
    EngineeringAttemptStartCommand,
    EngineeringAttemptStartOutcome,
    EngineeringDispatchCommand,
    EngineeringDispatchOutcome,
    EngineeringDispatchRead,
    EngineeringDispatchRefusal,
    EngineeringDispatchRepair,
)
from shared.contracts.dto.engineering_execution import (
    EngineeringExecutionPhase,
    infrastructure_refusal_for_dispatch,
)
from shared.contracts.dto.pr_conflict_repair import PRConflictRepairAttemptDisposition
from shared.contracts.dto.run import RunDTO, RunStatus
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskDTO, TaskStatus, TaskType
from shared.contracts.queues.engineering import EngineeringMessage
from shared.contracts.vocab import ActionType, OwnerNotificationEvent
from shared.pr_conflict_repair import settle_pr_repair_attempt
from shared.queues import ENGINEERING_QUEUE
from shared.redis import RedisStreamClient

from ._recipients import resolve_project_recipient
from .owner_notifications import (
    deliver_owed_notification,
    owe_owner_notification,
    owe_story_owner_notification,
)
from .story_completion import (
    _parse_owner_repo,
    _trigger_next_story,
    complete_stories,
)
from .worker_liveness import terminal_task_statuses

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient

__all__ = [
    "_build_cumulative_context",
    "_parse_owner_repo",
    "_trigger_next_story",
    "complete_stories",
    "dispatch_todo_tasks",
    "task_dispatcher_loop",
]

from .. import runtime, startup

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

    Ordinary outcomes follow the native start hops. Failed conflict attempts
    settle under their admission/Run fence even when dispatch never wrote the
    start; infrastructure/resource refusals defer to native supervision.

    The run is always finished by the time this is called: the admission point
    names this repair only for a run no longer in flight.
    """
    if task_id.startswith("pr-conflict-") and run.status is RunStatus.FAILED:
        result = run.result
        if result.allocation_failure_reason is not None or (
            result.execution is not None
            and result.execution.execution_phase is EngineeringExecutionPhase.PRE_AGENT_REFUSED
        ):
            # Restore only discovery; the next stuck sweep applies native
            # infrastructure/resource priority before any repair settlement.
            await _transition_to_in_dev(api_client, task_id, run.id, log)
            log.info("conflict_refusal_deferred_to_supervision", run_id=run.id)
            return
        if run.story_id is None:
            raise RuntimeError("An admitted conflict repair requires its story")
        outcome = await settle_pr_repair_attempt(
            api_client,
            run.story_id,
            task_id,
            run.id,
            "Recover terminal conflict Run before dispatch status write.",
            PRConflictRepairAttemptDisposition.FAILED,
        )
        log.info("conflict_dispatch_outcome_settled", run_id=run.id, outcome=outcome.outcome.value)
        return
    if task_id.startswith("pr-conflict-"):
        await _transition_to_in_dev(api_client, task_id, run.id, log)
        return
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
    if task.id.startswith("pr-conflict-"):
        started = await _transition_to_in_dev(api_client, task.id, decision.run_id, log)
        return started and decision.repair in _DISPATCH_COMPLETING
    await api_client.transition_task(task.id, TaskStatus.IN_DEV, "dispatcher")
    return decision.repair in _DISPATCH_COMPLETING


async def _handle_refusal(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    task: TaskDTO,
    decision: EngineeringDispatchRead,
    log: structlog.BoundLogger,
) -> None:
    """Consume the paid gate's disposition or route an ordinary Task refusal.

    `paid_work` is present exactly when the paid gate decided, and that is the
    line: a refusal from an earlier condition is a state this tick simply cannot
    dispatch in and a later tick may. The gate's no-Run decisions count no
    engineering attempt. Admission owns infrastructure and conflict stops;
    ordinary Tasks retain their existing scheduler refusal routing.
    """
    if decision.paid_work is None:
        log.info("task_dispatch_refused", reason=decision.reason.value)
        return
    admission = decision.paid_work.admission
    if infrastructure_refusal_for_dispatch(decision.reason) is not None:
        # Admission already parked this refusal, with its owed notices, in the
        # transaction that decided it. There is nothing left to sequence here,
        # and a second park call would only reopen the window that closed.
        log.info(
            "task_dispatch_infrastructure_refusal_parked_by_admission",
            run_id=decision.run_id,
            task_id=task.id,
            reason=decision.reason.value,
            disposition=(
                decision.infrastructure_park.value if decision.infrastructure_park else None
            ),
        )
        return
    if task.id.startswith("pr-conflict-"):
        disposition = decision.refusal_disposition
        if (
            disposition is None
            or disposition.task_id != task.id
            or disposition.reason is not decision.reason
        ):
            raise RuntimeError("Conflict paid refusal has no matching committed disposition")
        log.info(
            "task_dispatch_refusal_disposed_by_admission",
            task_id=disposition.task_id,
            decision_id=disposition.decision_id,
            reason=disposition.reason.value,
        )
        return
    if task.story_id and admission.message:
        await _park_refused_story(api_client, redis_client, task, decision, admission.message, log)
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


async def _initiating_run(
    api_client: SchedulerAPIClient,
    initiating_run_id: str,
    log: structlog.BoundLogger,
) -> RunDTO | None:
    """The Run this work was initiated by, or None when the id is not a Run's.

    A project created through the PO brief flow carries the id of the request
    the owner made, not of a Run — nothing was dispatched to produce it. The
    API answering 404 is the evidence for that, and the only thing it is taken
    as: any other failure is a failure to find out and is raised.
    """
    try:
        return await api_client.get_run(initiating_run_id)
    except httpx.HTTPStatusError as error:
        if error.response.status_code != httpx.codes.NOT_FOUND:
            raise
        log.info("task_refusal_initiator_is_not_a_run", initiating_run_id=initiating_run_id)
        return None


async def _park_refused_story(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    task: TaskDTO,
    decision: EngineeringDispatchRead,
    message: str,
    log: structlog.BoundLogger,
) -> None:
    """Hand the story to a human and tell its owner why it stopped moving.

    Where the durable record lives follows from what initiated the work. A Run
    keeps its own refusal, as it always has. A story whose initiator is a PO
    request has no Run to hang one on, so the record goes on the story — the
    same place the PR poller puts one for an ending nothing dispatched.
    """
    source_run = await _initiating_run(api_client, decision.initiating_run_id, log)
    if source_run is None:
        owed = await owe_story_owner_notification(
            api_client,
            task.story_id,
            # The event the PO prompt describes for exactly this ending: a
            # specialist has the story now. `story_quarantined`, which the
            # run-backed branch below sends, is not in the prompt's list.
            event=OwnerNotificationEvent.STORY_BLOCKED,
            text=message,
            project_id=str(task.project_id),
            terminal_status=StoryStatus.WAITING_HUMAN_REVIEW,
            log=log,
        )
        await api_client.transition_story(task.story_id, "human-review")
        await deliver_owed_notification(
            api_client, redis_client, task.story_id, owed, log, story_record=True
        )
        return
    owed = await owe_owner_notification(
        api_client,
        source_run,
        event=OwnerNotificationEvent.STORY_QUARANTINED,
        text=message,
        story_id=task.story_id,
        project_id=str(task.project_id),
        terminal_status=StoryStatus.WAITING_HUMAN_REVIEW,
        task_id=task.id,
        log=log,
    )
    await api_client.transition_story(task.story_id, "human-review")
    await deliver_owed_notification(api_client, redis_client, source_run.id, owed, log)


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

    async def start():
        if not task_id.startswith("pr-conflict-"):
            await api_client.transition_task(task_id, TaskStatus.IN_DEV, "dispatcher")
            return True
        decision = await api_client.start_engineering_attempt(
            EngineeringAttemptStartCommand(task_id=task_id, run_id=run_id)
        )
        log.info("conflict_attempt_start", run_id=run_id, outcome=decision.outcome.value)
        return decision.outcome in {
            EngineeringAttemptStartOutcome.STARTED,
            EngineeringAttemptStartOutcome.REUSED,
            EngineeringAttemptStartOutcome.COMPLETED,
        }

    try:
        return await start()
    except Exception:
        log.warning("task_transition_retry", run_id=run_id, exc_info=True)
        try:
            return await start()
        except Exception:
            log.exception("task_transition_failed", run_id=run_id)
            return False


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

        # One task's handling must not end the cycle: the candidates after it
        # are unrelated work, and a cycle that stops at the first broken task
        # leaves them in todo for as long as the failure lasts.
        try:
            if decision.outcome is EngineeringDispatchOutcome.REFUSED:
                await _handle_refusal(api_client, redis_client, task, decision, log)
            elif decision.outcome is EngineeringDispatchOutcome.REPAIR:
                if await _execute_repair(api_client, task, decision, log):
                    dispatched += 1
            elif await _publish_admitted_dispatch(api_client, redis_client, task, decision, log):
                dispatched += 1
        except Exception:
            log.exception("task_dispatch_handling_failed", task_id=task.id)
            continue

    return dispatched


async def task_dispatcher_loop() -> None:
    """Periodically dispatch admitted engineering tasks."""
    from ..clients.api import api_client
    async def cycle(redis_client: RedisStreamClient) -> dict[str, object]:
        return {"tasks_dispatched": await dispatch_todo_tasks(api_client, redis_client)}
    await runtime.periodic_loop(
        interval=_dispatch_interval, cycle=cycle, logger=logger,
        started_event="task_dispatcher_started", cycle_event="dispatcher_cycle",
        error_event="dispatcher_cycle_error", stopped_event="task_dispatcher_stopped",
        redis_factory=RedisStreamClient, sleep=asyncio.sleep,
    )

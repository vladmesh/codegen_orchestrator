"""Task Dispatcher — dispatches todo tasks, completes stories, supervises pipeline.

Responsibilities:
A) Find todo tasks with no blocker (or blocker done), create Run,
   publish to engineering:queue, transition task to in_dev.
B) Find stories where all tasks are done → complete story + trigger deploy.
C) Supervise pipeline: detect stuck states, retry or fail-fast.

Runs as a periodic scheduler job (every 30s).
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import TYPE_CHECKING
import uuid

import structlog

from shared.contracts.dto.engineering_budget_policy import EngineeringBudgetAdmissionOutcome
from shared.contracts.dto.project import (
    ProjectDTO,
    ProjectPredatesRunOwnership,
    ProjectStatus,
    require_initiating_run,
)
from shared.contracts.dto.run import RunDTO, RunStatus, RunType
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskDTO, TaskStatus, TaskType
from shared.contracts.dto.work_admission import PaidRunStartCommand, WorkAdmissionOutcome
from shared.contracts.queues.engineering import EngineeringMessage
from shared.contracts.vocab import ActionType, OwnerNotificationEvent
from shared.contracts.worker_turn import AttemptTurnMetadata
from shared.queues import ENGINEERING_QUEUE
from shared.redis_client import RedisStreamClient

from ._recipients import resolve_project_recipient
from .bot_rollouts import reconcile_bot_rollouts
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

# Statuses of a run that is still owned by the engineering pipeline: the worker
# either has not picked it up yet or is working on it.
_LIVE_RUN_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING)

# This is used only before calling the queue; a publication failure is never proof
# that the message did not land and must not be routed through the abort command.
PRE_HANDOFF_PREPARATION_FAILED_ERROR = "dispatch handoff preparation failed"


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


async def _find_unfinished_run(api_client: SchedulerAPIClient, task: TaskDTO) -> RunDTO | None:
    """Return an engineering run for this task that has not finished, any iteration.

    This is the guard that keeps one story branch to one worker, and it reads the
    only fact that answers the question: whether an attempt is still open. It
    deliberately ignores `current_iteration`. The supervisor increments that
    field in the same breath as it returns a task to todo, so a guard keyed on it
    stops recognising the very run whose worker may still be holding the branch —
    which is how a retry used to put a second worker on it.

    An attempt that is genuinely over is closed by whoever ended it: the result
    handler on a real outcome, and the supervisor on a stuck one, which fails the
    run before it fails the task. So a run left in queued/running always means
    work that is still owned, never a leftover to be dispatched past.
    """
    runs = await api_client.list_runs(task_id=task.id, run_type=RunType.ENGINEERING.value)
    for run in runs:
        if run.run_metadata.get("pre_handoff_aborted"):
            continue
        if run.status in _LIVE_RUN_STATUSES:
            return run
    return None


async def _find_dispatched_run(api_client: SchedulerAPIClient, task: TaskDTO) -> RunDTO | None:
    """Return the finished engineering run this task's current iteration produced.

    Only terminal runs reach here — an unfinished one is caught by
    `_find_unfinished_run` first. Its job is the replay case: a worker can finish
    before the next tick, and a task left in todo by a failed transition must
    have that outcome applied instead of being dispatched a second time. Runs of
    earlier iterations are ignored, because a legitimate retry has to be
    dispatchable.
    """
    runs = await api_client.list_runs(task_id=task.id, run_type=RunType.ENGINEERING.value)
    for run in runs:
        if run.run_metadata.get("pre_handoff_aborted"):
            continue
        if run.run_metadata.get("iteration") == task.current_iteration:
            return run
    return None


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

    The run is always finished by the time this is called: an unfinished one is
    adopted by the caller's guard before it gets here.
    """
    await api_client.transition_task(task_id, TaskStatus.IN_DEV, "dispatcher")
    for status in terminal_task_statuses(run):
        await api_client.transition_task(task_id, status, "dispatcher")
    log.info("task_outcome_replayed", run_id=run.id, run_status=run.status.value)


def _story_blocks_dispatch(siblings: list[TaskDTO], log: structlog.BoundLogger) -> bool:
    """Whether a sibling task in this story forbids dispatching another one.

    One task in flight per story, and none at all once a sibling has been handed
    to a human: a story branch is written by one worker at a time.
    """
    if any(sibling.status == TaskStatus.IN_DEV for sibling in siblings):
        log.info("task_skipped_story_busy")
        return True
    if any(sibling.status == TaskStatus.WAITING_HUMAN_REVIEW for sibling in siblings):
        log.info("task_skipped_story_has_gave_up_sibling")
        return True
    return False


class _PriorAttempt(StrEnum):
    """What an attempt this task already had made this tick do instead of dispatching."""

    #: This task's own dispatch, finished: the message went out on an earlier
    #: tick and only the transition was missing. Counts as dispatched.
    RECOVERED = "recovered"
    #: Somebody else's unfinished attempt is still holding the story branch —
    #: typically one the supervisor's retry path stepped over. The task goes back
    #: to in_dev and nothing is dispatched.
    BLOCKED = "blocked"
    #: A run that finished while the task was stuck in todo: its outcome is
    #: applied to the task instead of a second run being created.
    REPLAYED = "replayed"


#: Outcomes in which this tick left a task in in_dev with work behind it.
_DISPATCH_COMPLETING = (_PriorAttempt.RECOVERED, _PriorAttempt.REPLAYED)


async def _handle_prior_attempt(
    api_client: SchedulerAPIClient, task: TaskDTO, log: structlog.BoundLogger
) -> _PriorAttempt | None:
    """Deal with an attempt this task already has, or `None` if it has none.

    Unfinished first, and — this is the whole point — without consulting
    `current_iteration` to decide *whether* to stop. That field is incremented by
    the very retry that creates the risk, so a guard keyed on it stops
    recognising the run whose worker may still be holding the story branch.

    The iteration is read afterwards, and only to name what happened: an
    unfinished run of this iteration is this task's own dispatch being completed,
    one of an earlier iteration is a live attempt the retry path ran ahead of.
    Both take the same action; they are not the same event in the logs.
    """
    unfinished_run = await _find_unfinished_run(api_client, task)
    if unfinished_run is not None:
        run_iteration = unfinished_run.run_metadata.get("iteration")
        own_dispatch = run_iteration == task.current_iteration
        event = (
            "task_transition_recovered" if own_dispatch else "task_dispatch_blocked_by_live_attempt"
        )
        log.info(
            event,
            run_id=unfinished_run.id,
            run_status=unfinished_run.status.value,
            iteration=task.current_iteration,
            run_iteration=run_iteration,
        )
        await api_client.transition_task(task.id, TaskStatus.IN_DEV, "dispatcher")
        return _PriorAttempt.RECOVERED if own_dispatch else _PriorAttempt.BLOCKED

    prior_run = await _find_dispatched_run(api_client, task)
    if prior_run is not None:
        await _recover_dispatched_task(api_client, task.id, prior_run, log)
        return _PriorAttempt.REPLAYED
    return None


async def _create_and_publish_run(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    task: TaskDTO,
    description: str,
    initiating_run_id: str,
    log: structlog.BoundLogger,
) -> str | None:
    """Create the engineering run and publish its message.

    `initiating_run_id` is the project's — the run that asked for this work.
    This function creates one *attempt* inside it, and the message carries both:
    the attempt as `task_id`, the run as `initiating_run_id`.

    Returns the run id. Only a failure before attempting queue handoff releases the
    reservation. A created run is closed as CANCELLED because nothing can pick it up; the task stays
    in todo and a later tick may make a fresh attempt. A budget denial instead routes
    the task to human review and cannot retry automatically.
    """
    task_id = task.id
    story_id = task.story_id
    project_id = str(task.project_id)

    run_id = f"eng-{uuid.uuid4().hex[:12]}"
    run_metadata = {
        "triggered_by": "dispatcher",
        "story_id": story_id,
        "task_id": task_id,
        **AttemptTurnMetadata(initiating_run_id=initiating_run_id).as_run_metadata(),
        "iteration": task.current_iteration,
    }
    try:
        started = await api_client.start_paid_run(
            PaidRunStartCommand(
                id=run_id,
                type=RunType.ENGINEERING,
                project_id=task.project_id,
                task_id=task_id,
                story_id=story_id,
                run_metadata=run_metadata,
            )
        )
    except Exception:
        log.exception("task_dispatch_paid_start_failed", run_id=run_id)
        return None
    if started.admission.outcome is not WorkAdmissionOutcome.ADMITTED:
        if task.story_id and started.admission.message:
            source_run = await api_client.get_run(initiating_run_id)
            owed = await owe_owner_notification(
                api_client,
                source_run,
                event=OwnerNotificationEvent.STORY_QUARANTINED,
                text=started.admission.message,
                story_id=task.story_id,
                project_id=project_id,
                terminal_status=StoryStatus.WAITING_HUMAN_REVIEW,
                task_id=task_id,
                log=log,
            )
            await api_client.transition_story(task.story_id, "human-review")
            await deliver_owed_notification(api_client, redis_client, source_run.id, owed, log)
        budget = started.engineering_budget
        if budget is not None and budget.outcome is EngineeringBudgetAdmissionOutcome.DENIED:
            await api_client.transition_task(task_id, TaskStatus.IN_DEV, "dispatcher")
            await api_client.transition_task(
                task_id,
                TaskStatus.WAITING_HUMAN_REVIEW,
                "dispatcher",
                details={
                    "reason": "engineering_budget_denied",
                    "attempt_id": budget.attempt_id,
                    "known_spend_microusd": budget.known_spend_microusd,
                    "active_held_microusd": budget.active_held_microusd,
                    "available_microusd": budget.available_microusd,
                },
            )
        else:
            await api_client.transition_task(task_id, TaskStatus.IN_DEV, "dispatcher")
            await api_client.transition_task(
                task_id,
                TaskStatus.WAITING_HUMAN_REVIEW,
                "dispatcher",
                details={
                    "reason": (
                        started.admission.reason.value
                        if started.admission.reason is not None
                        else "paid_work_denied"
                    ),
                    "attempt_id": run_id,
                },
            )
        log.info(
            "task_dispatch_count_admission_refused",
            run_id=run_id,
            task_id=task_id,
            reason=(
                started.admission.reason.value if started.admission.reason is not None else None
            ),
        )
        return None
    try:
        action = ActionType.FEATURE if task.type is TaskType.REFACTOR else ActionType(task.type)
        recipient = await resolve_project_recipient(
            api_client, project_id, event="task_dispatch", story_id=story_id or ""
        )
        eng_msg = EngineeringMessage(
            task_id=run_id,
            project_id=project_id,
            initiating_run_id=initiating_run_id,
            telegram_chat_id=recipient.telegram_chat_id,
            action=action,
            description=description,
            skip_deploy=True,  # Deploy handled at story level
            planning_task_id=task_id,
            story_id=story_id,
            branch=f"story/{story_id}" if story_id else None,
        )
    except Exception:
        # This block contains only work proven to precede any queue call.  Do
        # not include publication: a lost publish response has an unknown outcome.
        log.exception("task_dispatch_pre_handoff_preparation_failed", run_id=run_id)
        await api_client.abort_paid_run_pre_handoff(run_id, PRE_HANDOFF_PREPARATION_FAILED_ERROR)
        return None
    try:
        await redis_client.publish_message(ENGINEERING_QUEUE, eng_msg)
    except Exception:
        # Publication may have reached Redis before its response was lost.  Keep
        # the queued Run and active hold so normal unfinished-run recovery owns it.
        log.exception("task_dispatch_publish_outcome_unknown", run_id=run_id)
        return None
    return run_id


async def _transition_to_in_dev(
    api_client: SchedulerAPIClient,
    task_id: str,
    run_id: str,
    log: structlog.BoundLogger,
) -> bool:
    """Move a task to in_dev, retrying once.

    The message is already out, so the run is live and the task must not stay in
    todo. If both attempts fail, the pre-dispatch guard finishes the transition on
    the next tick.
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


async def _project_and_initiating_run(
    api_client: SchedulerAPIClient, project_id: str, log: structlog.BoundLogger
) -> tuple[ProjectDTO, str] | None:
    """The project and the run that will own its worker, or None to skip the task.

    Both reasons to skip say the same thing — there is nothing to attribute the
    worker to — so they are answered together. A project written before run
    ownership existed names no run and none can be reconstructed for it, so it is
    skipped loudly rather than dispatched into a worker nobody could attribute
    after it dies.
    """
    project = await api_client.get_project(project_id)
    if project is None:
        log.error("task_skipped_project_missing", project_id=project_id)
        return None
    try:
        return project, require_initiating_run(project)
    except ProjectPredatesRunOwnership as exc:
        log.error(
            "task_skipped_project_has_no_initiating_run",
            project_id=project_id,
            reason=str(exc),
        )
        return None


async def dispatch_todo_tasks(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> int:
    """Find and dispatch unblocked todo tasks.

    Returns the number of tasks dispatched.
    """
    tasks = await api_client.get_tasks_by_status(TaskStatus.TODO)
    dispatched = 0

    for task in tasks:
        task_id = task.id
        blocker_id = task.blocked_by_task_id

        # Check if blocker is resolved
        if blocker_id:
            blocker = await api_client.get_task(blocker_id)
            if blocker.status != TaskStatus.DONE:
                continue  # Still blocked

        story_id = task.story_id
        project_id = str(task.project_id)
        log = logger.bind(task_id=task_id, story_id=story_id)

        # Skip internal project tasks — implemented manually via /implement
        # TODO: replace with proper project.internal flag when going to prod
        INTERNAL_PROJECT_ID = "033c2033-fc75-4d86-ade2-08efe7b15a5e"
        if project_id == INTERNAL_PROJECT_ID:
            continue

        # The project is read once and kept: it decides whether this task may be
        # dispatched at all, and it carries the run that initiated the work,
        # which the message below has to hand on to the worker.
        resolved = await _project_and_initiating_run(api_client, project_id, log)
        if resolved is None:
            continue
        project, initiating_run_id = resolved

        # Guard: don't dispatch until scaffold is complete and workspace is ready
        if project.status == ProjectStatus.DRAFT:
            log.info("task_skipped_not_scaffolded", project_status=project.status)
            continue
        if not (project.config or {}).get("workspace_ready"):
            log.info("task_skipped_workspace_not_ready", project_id=project_id)
            continue

        # Fetch siblings once — used for both guard and context
        siblings = await api_client.get_tasks_by_story(story_id) if story_id else []
        if _story_blocks_dispatch(siblings, log):
            continue

        # This task may already have an attempt — one still running, or one that
        # finished before its outcome could be applied. Either way it must not
        # get a second worker on the same story branch.
        prior = await _handle_prior_attempt(api_client, task, log)
        if prior is not None:
            if prior in _DISPATCH_COMPLETING:
                dispatched += 1
            continue

        # Build cumulative context from sibling tasks
        context = ""
        if siblings:
            all_events = []
            for sibling in siblings:
                if sibling.id != task_id and sibling.status == TaskStatus.DONE:
                    events = await api_client.get_task_events(sibling.id)
                    all_events.extend(events)
            context = _build_cumulative_context(all_events)

        # Enrich description with context
        description = task.description or ""
        if context:
            description = context + description

        run_id = await _create_and_publish_run(
            api_client, redis_client, task, description, initiating_run_id, log
        )
        if run_id is None:
            continue

        if not await _transition_to_in_dev(api_client, task_id, run_id, log):
            continue

        log.info("task_dispatched", run_id=run_id)
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
                # Rollout reconciliation runs after the routing that can publish
                # new work: a record owed by this tick is attempted next tick,
                # which is the same one-attempt-per-tick pacing the owner
                # notification sweep above gets.
                bot_rollouts = await reconcile_bot_rollouts(api_client, redis_client)

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
                    + bot_rollouts["published"]
                    + bot_rollouts["publish_retrying"]
                    + bot_rollouts["publish_exhausted"]
                    + bot_rollouts["notified"]
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
                        # Bot-audience rollouts: publish recovered from a
                        # committed-but-unpublished staging vs. still being
                        # retried vs. given up and handed to a human; plus the
                        # promised terminal outcomes delivered this tick.
                        bot_rollout_published=bot_rollouts["published"],
                        bot_rollout_publish_retrying=bot_rollouts["publish_retrying"],
                        bot_rollout_publish_exhausted=bot_rollouts["publish_exhausted"],
                        bot_rollout_notified=bot_rollouts["notified"],
                    )
            except Exception:
                logger.exception("dispatcher_cycle_error")
            await asyncio.sleep(_dispatch_interval())
    finally:
        await redis_client.close()
        logger.info("task_dispatcher_stopped")

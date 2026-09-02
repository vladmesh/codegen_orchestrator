"""The declared admission point for paid engineering dispatch.

Every dispatch of an engineering Task passes through `admit_engineering_dispatch`
and gets back one typed decision with one typed refusal reason. The conditions
used to live inline in the scheduler and were evaluated client-side, from data
fetched over HTTP between the reads; here they are one ordered sequence over rows
locked in the transaction that decides, ending in the budget/slot gate that was
already server-side. That gate is wrapped, not duplicated: `start_paid_run` is
still the only thing that may create a queued paid run.

A new condition — the Product Brief admission is the next one — is one more step
in `admit_engineering_dispatch` and one more value in `EngineeringDispatchRefusal`.
It is not a new surface.

Lock order is Task rows first — the candidate and, when it has one, its blocker,
taken in ascending task id, never in dependency order — then the story, then the
paid-work control rows `start_paid_run` takes. Ascending id is what makes a
reciprocal dependency safe: two dispatches of tasks that block each other take
the same two rows in the same order, so one waits instead of both deadlocking.
Nothing in this service locks a story before a task, so this order adds no cycle
to the existing ones.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.engineering_dispatch import (
    PAID_WORK_REFUSALS,
    EngineeringDispatchCommand,
    EngineeringDispatchOutcome,
    EngineeringDispatchRead,
    EngineeringDispatchRefusal,
    EngineeringDispatchRepair,
)
from shared.contracts.dto.project import (
    ProjectPredatesRunOwnership,
    ProjectStatus,
    require_initiating_run,
)
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.task import TaskStatus
from shared.contracts.dto.work_admission import PaidRunStartCommand, WorkAdmissionOutcome
from shared.contracts.worker_turn import AttemptTurnMetadata
from shared.models import Project, Run, Task

from .work_admission import start_paid_run

#: The orchestrator's own project. Its tasks are implemented by hand, so the
#: dispatcher must never buy a worker for one. Held here as one explicit
#: condition of the admission point rather than as a constant in the scheduler;
#: a real project flag is a product change and not this module's to invent.
INTERNAL_PROJECT_ID = uuid.UUID("033c2033-fc75-4d86-ade2-08efe7b15a5e")

#: Statuses of a run the engineering pipeline still owns: the worker either has
#: not picked it up yet or is working on it.
_LIVE_RUN_STATUSES = (RunStatus.QUEUED.value, RunStatus.RUNNING.value)


class _Overrides:
    """The conditions this caller may walk past, and the ones it actually did.

    Every condition is still evaluated. An override only changes what happens
    when one refuses, and it is recorded both ways: on the decision and, for an
    admitted dispatch, on the attempt the admission point creates.
    """

    def __init__(self, command: EngineeringDispatchCommand) -> None:
        self._allowed = set(command.overrides)
        self.applied: list[EngineeringDispatchRefusal] = []

    def clears(self, reason: EngineeringDispatchRefusal) -> bool:
        if reason not in self._allowed:
            return False
        if reason not in self.applied:
            self.applied.append(reason)
        return True


def _refused(reason: EngineeringDispatchRefusal, overrides: _Overrides) -> EngineeringDispatchRead:
    return EngineeringDispatchRead(
        outcome=EngineeringDispatchOutcome.REFUSED,
        reason=reason,
        overridden=list(overrides.applied),
    )


async def _engineering_attempts(task: Task, db: AsyncSession) -> list[Run]:
    """This task's engineering runs, newest first, minus the aborted ones.

    A run aborted before queue handoff is proof that no message reached a worker,
    so it is not an attempt anything can be recovered from.
    """
    runs = (
        await db.scalars(
            select(Run)
            .where(Run.task_id == task.id, Run.type == RunType.ENGINEERING.value)
            .order_by(Run.created_at.desc())
        )
    ).all()
    return [run for run in runs if not (run.run_metadata or {}).get("pre_handoff_aborted")]


async def _story_has_live_engineering_run(task: Task, db: AsyncSession) -> bool:
    """Does a sibling of this task still hold a live engineering run?

    Read under the story lock, and deliberately about runs rather than statuses:
    the status write that would have made a sibling visible as `in_dev` locks only
    its own row, so a published attempt whose transition failed is still `todo`.
    Its run is not. The candidate's own runs are excluded — an attempt of the task
    being admitted is the prior-attempt fence's subject, and it answers with a
    repair rather than with "the story is busy".
    """
    runs = (
        await db.scalars(
            select(Run)
            .join(Task, Task.id == Run.task_id)
            .where(
                Task.story_id == task.story_id,
                Task.id != task.id,
                Run.type == RunType.ENGINEERING.value,
                Run.status.in_(_LIVE_RUN_STATUSES),
            )
        )
    ).all()
    # A run aborted before queue handoff never reached a worker, so it holds
    # nothing — the same rule `_engineering_attempts` applies to the candidate.
    return any(not (run.run_metadata or {}).get("pre_handoff_aborted") for run in runs)


def _prior_attempt(
    task: Task, attempts: list[Run], initiating_run_id: str
) -> EngineeringDispatchRead | None:
    """The repair an attempt this task already has still owes, or None.

    Unfinished first, and — this is the whole point — without consulting
    `current_iteration` to decide *whether* to stop. That field is incremented by
    the very retry that creates the risk, so a fence keyed on it stops
    recognising the run whose worker may still be holding the story branch, which
    is how a retry used to put a second worker on it. An attempt that is
    genuinely over is closed by whoever ended it, so a run left in queued/running
    always means work that is still owned.

    The iteration is read afterwards, and only to name what happened: a live run
    of this iteration is this task's own dispatch being completed, one of an
    earlier iteration is a live attempt the retry path ran ahead of. Both take
    the same action; they are not the same event.
    """
    for run in attempts:
        if run.status not in _LIVE_RUN_STATUSES:
            continue
        own_dispatch = (run.run_metadata or {}).get("iteration") == task.current_iteration
        return EngineeringDispatchRead(
            outcome=EngineeringDispatchOutcome.REPAIR,
            repair=(
                EngineeringDispatchRepair.RECOVER_OWN_ATTEMPT
                if own_dispatch
                else EngineeringDispatchRepair.ADOPT_LIVE_ATTEMPT
            ),
            reason=None if own_dispatch else EngineeringDispatchRefusal.LIVE_ATTEMPT_IN_FLIGHT,
            run_id=run.id,
            initiating_run_id=initiating_run_id,
        )
    # Only terminal runs reach here. This is the replay case: a worker can finish
    # before the next tick, and a task a failed transition left in todo must have
    # that outcome applied instead of being dispatched a second time. Runs of
    # earlier iterations are ignored, because a legitimate retry has to be
    # dispatchable.
    for run in attempts:
        if (run.run_metadata or {}).get("iteration") == task.current_iteration:
            return EngineeringDispatchRead(
                outcome=EngineeringDispatchOutcome.REPAIR,
                repair=EngineeringDispatchRepair.REPLAY_FINISHED_RUN,
                run_id=run.id,
                initiating_run_id=initiating_run_id,
            )
    return None


async def admit_engineering_dispatch(
    command: EngineeringDispatchCommand, db: AsyncSession
) -> EngineeringDispatchRead:
    """Decide whether one engineering Task may be dispatched, and admit it if so.

    An ADMITTED decision has already created the queued Run and taken its budget
    hold, exactly as `start_paid_run` did when the scheduler called it directly:
    the caller still owes the queue handoff and the transition out of todo, and
    still compensates a failed handoff through `abort_paid_run_pre_handoff`.

    A REPAIR decision creates nothing. The repair is named, not performed, so a
    decision never hides a side effect; the caller executes it through the same
    transition endpoints it used before.
    """
    # Deferred: `src.routers` imports this module for its endpoint, and the
    # locking row readers card 1237 declared live under it. Importing them here
    # keeps that one cycle out of module import order.
    from .routers._story_helpers import _get_story_for_update
    from .routers._task_helpers import get_task_for_update

    overrides = _Overrides(command)

    # Both Task rows this decision may read are taken here, in ascending id, so
    # a pair of tasks that block each other cannot be locked in opposite orders
    # by two concurrent dispatches. The edge is peeked at first, unlocked and
    # with a column-only query that puts no stale entity in the session, purely
    # to learn which second row to take.
    peeked_blocker_id = await db.scalar(
        select(Task.blocked_by_task_id).where(Task.id == command.task_id)
    )
    locked: dict[str, Task] = {}
    for task_id in sorted({command.task_id, peeked_blocker_id} - {None}):
        locked[task_id] = await get_task_for_update(task_id, db)
    task = locked[command.task_id]

    if task.status != TaskStatus.TODO.value and not overrides.clears(
        EngineeringDispatchRefusal.TASK_NOT_DISPATCHABLE
    ):
        return _refused(EngineeringDispatchRefusal.TASK_NOT_DISPATCHABLE, overrides)

    if task.blocked_by_task_id:
        blocker = locked.get(task.blocked_by_task_id)
        # The locked row names a blocker the peek did not: the edge was rewritten
        # between the two reads. Taking it now would lock out of order, so this
        # tick refuses and the next one peeks at the edge that exists.
        blocker_resolved = blocker is not None and blocker.status == TaskStatus.DONE.value
        if not blocker_resolved and not overrides.clears(
            EngineeringDispatchRefusal.BLOCKER_UNRESOLVED
        ):
            return _refused(EngineeringDispatchRefusal.BLOCKER_UNRESOLVED, overrides)

    if task.project_id == INTERNAL_PROJECT_ID and not overrides.clears(
        EngineeringDispatchRefusal.INTERNAL_PROJECT
    ):
        return _refused(EngineeringDispatchRefusal.INTERNAL_PROJECT, overrides)

    # The project decides whether this task may be dispatched at all, and it
    # carries the run that initiated the work, which the message has to hand on
    # to the worker.
    project = await db.scalar(select(Project).where(Project.id == task.project_id))
    if project is None:
        # Unreachable through the schema — `tasks.project_id` is a non-nullable
        # foreign key — so this is a broken database, not a refusal to name.
        raise RuntimeError(f"Project {task.project_id} does not exist")
    try:
        initiating_run_id = require_initiating_run(project)
    except ProjectPredatesRunOwnership:
        return _refused(EngineeringDispatchRefusal.PROJECT_HAS_NO_INITIATING_RUN, overrides)
    if project.status == ProjectStatus.DRAFT.value:
        return _refused(EngineeringDispatchRefusal.PROJECT_NOT_SCAFFOLDED, overrides)
    if not (project.config or {}).get("workspace_ready"):
        return _refused(EngineeringDispatchRefusal.WORKSPACE_NOT_READY, overrides)

    if task.story_id:
        # The story row is the fence: two tasks of one story lock their own rows
        # and would otherwise both read a sibling list without the other in it.
        # Taking the story serializes them, so the scan below decides on a state
        # nobody is concurrently adding a second worker to.
        await _get_story_for_update(task.story_id, db)
        # Siblings, not the candidate: this condition asks what *else* holds the
        # branch. A caller that overrode the dispatchability status — an operator
        # respawning a worker for a task already in_dev — must not be refused by
        # its own row showing up in the scan.
        siblings = (
            await db.scalars(select(Task).where(Task.story_id == task.story_id, Task.id != task.id))
        ).all()
        # One task in flight per story, and none at all once a sibling has been
        # handed to a human: a story branch is written by one worker at a time.
        busy = any(sibling.status == TaskStatus.IN_DEV.value for sibling in siblings)
        # A sibling's status is not the whole of "in flight". A sibling that was
        # admitted and published but whose transition out of todo failed is a
        # supported recovery state: still todo, with a live run whose worker holds
        # the story branch. The fence therefore observes the runs as well, and
        # refuses the same way — this is one condition, not two.
        busy = busy or await _story_has_live_engineering_run(task, db)
        if busy and not overrides.clears(EngineeringDispatchRefusal.STORY_BUSY):
            return _refused(EngineeringDispatchRefusal.STORY_BUSY, overrides)
        if any(
            sibling.status == TaskStatus.WAITING_HUMAN_REVIEW.value for sibling in siblings
        ) and not overrides.clears(EngineeringDispatchRefusal.STORY_WAITING_HUMAN_REVIEW):
            return _refused(EngineeringDispatchRefusal.STORY_WAITING_HUMAN_REVIEW, overrides)

    # Evaluated whether or not the caller may walk past it, so an override is
    # recorded only when it actually walked past something.
    attempts = await _engineering_attempts(task, db)
    prior = _prior_attempt(task, attempts, initiating_run_id)
    if prior is not None and not overrides.clears(
        EngineeringDispatchRefusal.LIVE_ATTEMPT_IN_FLIGHT
    ):
        return prior.model_copy(update={"overridden": list(overrides.applied)})

    run_id = f"eng-{uuid.uuid4().hex[:12]}"
    started = await start_paid_run(
        PaidRunStartCommand(
            id=run_id,
            type=RunType.ENGINEERING,
            project_id=task.project_id,
            task_id=task.id,
            story_id=task.story_id,
            run_metadata={
                "triggered_by": command.origin.value,
                "story_id": task.story_id,
                "task_id": task.id,
                **AttemptTurnMetadata(initiating_run_id=initiating_run_id).as_run_metadata(),
                "iteration": task.current_iteration,
                # The audit trail of an override: the attempt itself records which
                # refusals it only exists because somebody was authorised to walk
                # past. Absent from every scheduled dispatch.
                **(
                    {"admission_overrides": [r.value for r in overrides.applied]}
                    if overrides.applied
                    else {}
                ),
            },
        ),
        db,
    )
    if started.admission.outcome is not WorkAdmissionOutcome.ADMITTED:
        reason = started.admission.reason
        if reason is None:
            # Every refusal here has to carry a typed reason. A paid decision
            # without one is a broken decision, not a refusal to name.
            raise RuntimeError(f"Paid work refused {run_id} without a reason")
        return EngineeringDispatchRead(
            outcome=EngineeringDispatchOutcome.REFUSED,
            reason=PAID_WORK_REFUSALS[reason],
            run_id=run_id,
            initiating_run_id=initiating_run_id,
            paid_work=started,
            overridden=list(overrides.applied),
        )
    return EngineeringDispatchRead(
        outcome=EngineeringDispatchOutcome.ADMITTED,
        run_id=started.run_id,
        initiating_run_id=initiating_run_id,
        paid_work=started,
        overridden=list(overrides.applied),
    )

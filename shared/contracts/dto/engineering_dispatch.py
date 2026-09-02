"""The typed question and answer of engineering dispatch admission.

One command in, one decision out. The decision is what the dispatcher acts on:
it never re-derives an admission condition of its own from the DTOs it happens to
hold, because every condition was decided server-side on locked rows.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .work_admission import PaidRunStartRead, WorkAdmissionReason


class EngineeringDispatchOutcome(StrEnum):
    """What this tick may do with the task it asked about."""

    #: The paid attempt exists and is counted: publish its message and move on.
    ADMITTED = "admitted"
    #: The task already has an attempt behind it. Nothing new was created; the
    #: caller executes the named repair instead of dispatching.
    REPAIR = "repair"
    #: Do not dispatch. `reason` says why.
    REFUSED = "refused"


class EngineeringDispatchRepair(StrEnum):
    """The transition work an attempt this task already has still owes.

    Named rather than performed: the admission point decides, the dispatcher
    executes, so a decision never carries a hidden side effect. Which repair it
    is never depends on `current_iteration` — see `EngineeringDispatchRead`.
    """

    #: This task's own dispatch, still live: the message went out on an earlier
    #: tick and only the transition out of todo was missing. Counts as dispatched.
    RECOVER_OWN_ATTEMPT = "recover_own_attempt"
    #: Somebody else's live attempt is still holding the story branch — typically
    #: one the supervisor's retry path stepped over. The task is put back in
    #: in_dev and nothing is dispatched.
    ADOPT_LIVE_ATTEMPT = "adopt_live_attempt"
    #: A run that finished while the task was stuck in todo: its outcome is
    #: applied to the task instead of a second run being created.
    REPLAY_FINISHED_RUN = "replay_finished_run"


class EngineeringDispatchRefusal(StrEnum):
    """One value per distinct reason a dispatch was refused.

    An operator reading a refusal has to be able to tell "the story is busy" from
    "the workspace is not ready" from "the budget said no" without parsing a log
    line, so no two conditions share a value and none of them is a bare bool.
    """

    #: The locked row is no longer in todo — somebody moved it while this tick
    #: was reading the candidate list.
    TASK_NOT_DISPATCHABLE = "task_not_dispatchable"
    #: The orchestrator's own project, whose tasks are implemented by hand.
    INTERNAL_PROJECT = "internal_project"
    #: `blocked_by_task_id` names a task that is not done.
    BLOCKER_UNRESOLVED = "blocker_unresolved"
    #: A project written before run ownership existed: no run to attribute a
    #: worker to, and none can be reconstructed.
    PROJECT_HAS_NO_INITIATING_RUN = "project_has_no_initiating_run"
    #: Still a draft — the scaffold has not run.
    PROJECT_NOT_SCAFFOLDED = "project_not_scaffolded"
    WORKSPACE_NOT_READY = "workspace_not_ready"
    #: A sibling task of this story is in_dev: one worker per story branch.
    STORY_BUSY = "story_busy"
    #: A sibling was handed to a human, so the story takes no new work at all.
    STORY_WAITING_HUMAN_REVIEW = "story_waiting_human_review"
    #: An attempt is still open on this task; the decision carries the repair.
    LIVE_ATTEMPT_IN_FLIGHT = "live_attempt_in_flight"
    #: The paid gate refused. One value per `WorkAdmissionReason` that reaches
    #: engineering dispatch, so the refusal survives without the caller reading
    #: into `paid_work`.
    EMERGENCY_STOP = "emergency_stop"
    PAID_WORK_LIMIT = "paid_work_limit"
    ENGINEERING_BUDGET_DENIED = "engineering_budget_denied"
    EXECUTOR_UNAVAILABLE = "executor_unavailable"
    EXECUTOR_CONFIRMATION_REQUIRED = "executor_confirmation_required"


#: The paid gate's reasons, translated into this vocabulary. Complete over
#: `WorkAdmissionReason` minus `PROJECT_LIMIT`, which belongs to project creation
#: and can never be returned by a paid-run start.
PAID_WORK_REFUSALS: dict[WorkAdmissionReason, EngineeringDispatchRefusal] = {
    WorkAdmissionReason.EMERGENCY_STOP: EngineeringDispatchRefusal.EMERGENCY_STOP,
    WorkAdmissionReason.PAID_WORK_LIMIT: EngineeringDispatchRefusal.PAID_WORK_LIMIT,
    WorkAdmissionReason.ENGINEERING_BUDGET_DENIED: (
        EngineeringDispatchRefusal.ENGINEERING_BUDGET_DENIED
    ),
    WorkAdmissionReason.EXECUTOR_UNAVAILABLE: EngineeringDispatchRefusal.EXECUTOR_UNAVAILABLE,
    WorkAdmissionReason.EXECUTOR_CONFIRMATION_REQUIRED: (
        EngineeringDispatchRefusal.EXECUTOR_CONFIRMATION_REQUIRED
    ),
}


class EngineeringDispatchCommand(BaseModel):
    """Ask whether one engineering Task may be dispatched now.

    The task id is the whole command: every fact the decision needs is read
    server-side, on the locked rows, inside the deciding transaction. A caller
    that could pass the project status or the sibling list would be a caller that
    could pass a stale one.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)


class EngineeringDispatchRead(BaseModel):
    """The admission point's answer for one task.

    `run_id` names the engineering run this decision is about: the one just
    created when admitted, the one the paid gate refused to create when the
    refusal came from that gate, and the prior attempt's run when repairing.

    A repair decision is deliberately not keyed on `current_iteration`. That
    field is incremented by the very retry that creates the risk, so a fence
    keyed on it stops recognising the run whose worker may still be holding the
    story branch. The iteration is read only to *name* which repair it is, never
    to decide whether to stop.
    """

    model_config = ConfigDict(extra="forbid")

    outcome: EngineeringDispatchOutcome
    reason: EngineeringDispatchRefusal | None = None
    repair: EngineeringDispatchRepair | None = None
    run_id: str | None = None
    #: The run that asked for this work, read off the project. The engineering
    #: message hands it on to the worker.
    initiating_run_id: str | None = None
    #: The wrapped paid-work decision, present exactly when the paid gate was
    #: reached. Every earlier condition refuses before anything is counted.
    paid_work: PaidRunStartRead | None = None

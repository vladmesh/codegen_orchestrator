"""The typed question and answer of engineering dispatch admission.

One command in, one decision out. The decision is what the dispatcher acts on:
it never re-derives an admission condition of its own from the DTOs it happens to
hold, because every condition was decided server-side on locked rows.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


class EngineeringDispatchOrigin(StrEnum):
    """Who is asking. Recorded on the run the admission point creates.

    The conditions are the same for both; the origin only names the caller in
    the attempt's metadata, so an operator reading a run can tell a scheduled
    dispatch from one a human asked for.
    """

    DISPATCHER = "dispatcher"
    ADMIN = "admin"


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
    #: The task was planned against a Product Brief whose coverage has not been
    #: admitted yet. A `todo` status is not dispatch authority for brief-backed
    #: work: the plan is released as a whole, by the brief's one admission step,
    #: and until then no task of it may be bought.
    PRODUCT_BRIEF_NOT_ADMITTED = "product_brief_not_admitted"
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
    #: The story's roster changed under the decision: a task was inserted into
    #: the story, or moved into it, after the admission point had chosen which
    #: rows to lock. A row lock fences updates, not inserts, so the roster is
    #: re-read under the story lock and a member the decision does not hold ends
    #: the tick rather than being taken out of ladder order. The next tick sees
    #: the roster that exists.
    STORY_ROSTER_CHANGED = "story_roster_changed"
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


#: The typed error `POST /work-admission/paid-runs` returns for a paid
#: engineering start whose `task_id` names an existing Task row. That command is
#: a Task dispatch, and a Task dispatch is admitted in exactly one place — every
#: other paid start still goes through the paid gate unchanged, including the
#: deploy-fix handoff, which names no Task row.
ENGINEERING_TASK_REQUIRES_ADMISSION = "engineering_task_dispatch_requires_admission"


#: The typed error `POST /work-admission/paid-runs` returns when an engineering
#: command names no Task with a non-null `task_id`. Story-owned deploy fixes
#: leave that field null; an unknown reference is refused before Run creation.
ENGINEERING_TASK_NOT_FOUND = "engineering_task_not_found"


#: The refusals an authorised operator may override, and the only values a
#: command's `overrides` may contain. Everything absent from this set is refused
#: no matter who asks: the paid gate's decisions, because overriding them would
#: spend money nobody admitted, and the project conditions, because a worker sent
#: at a draft or unprepared workspace has nothing to check out.
OVERRIDABLE_REFUSALS: frozenset[EngineeringDispatchRefusal] = frozenset(
    {
        EngineeringDispatchRefusal.TASK_NOT_DISPATCHABLE,
        EngineeringDispatchRefusal.INTERNAL_PROJECT,
        EngineeringDispatchRefusal.BLOCKER_UNRESOLVED,
        EngineeringDispatchRefusal.STORY_BUSY,
        EngineeringDispatchRefusal.STORY_WAITING_HUMAN_REVIEW,
        EngineeringDispatchRefusal.LIVE_ATTEMPT_IN_FLIGHT,
    }
)


class EngineeringDispatchCommand(BaseModel):
    """Ask whether one engineering Task may be dispatched now.

    The task id is the whole of the question: every fact the decision needs is
    read server-side, on the locked rows, inside the deciding transaction. A
    caller that could pass the project status or the sibling list would be a
    caller that could pass a stale one. The other two fields say nothing about
    the state — `origin` names who is asking, and `overrides` names, one typed
    value at a time, the refusals this caller is authorised to walk past.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    origin: EngineeringDispatchOrigin = EngineeringDispatchOrigin.DISPATCHER
    #: Conditions this caller is authorised to override, named one by one. An
    #: empty list — the default, and the only thing the scheduler ever sends — is
    #: full admission. An override is not an absence of admission: the condition
    #: is still evaluated, the decision reports it in `overridden`, and the
    #: attempt records it, so a dispatch that only happened because a human said
    #: so says which condition it walked past.
    overrides: list[EngineeringDispatchRefusal] = Field(default_factory=list)

    @field_validator("overrides")
    @classmethod
    def _only_overridable(
        cls, overrides: list[EngineeringDispatchRefusal]
    ) -> list[EngineeringDispatchRefusal]:
        forbidden = [r for r in overrides if r not in OVERRIDABLE_REFUSALS]
        if forbidden:
            raise ValueError("not overridable: " + ", ".join(sorted(r.value for r in forbidden)))
        return overrides


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
    #: The conditions that refused and were walked past because the command
    #: named them. Empty for every scheduled dispatch; the audit trail of an
    #: operator override, and written onto the attempt it created.
    overridden: list[EngineeringDispatchRefusal] = Field(default_factory=list)

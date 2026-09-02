"""One story branch, one worker — including across the supervisor's retry tick.

The failure this pins down: `supervise_failed_tasks` returns a task to todo and
increments `current_iteration` in the same action. The dispatch fence used to
match a run on that very field, so the moment it was incremented the previous
run — whose worker may still be writing to `story/<id>` — stopped counting as a
reason not to dispatch, and the next tick started a second attempt beside it.

The fence moved into the admission point in card 1240 and stayed pure: it reads
the task and its runs and *names* the repair, never performing one. So this is a
unit test of that function, and the property is the same one the scheduler test
of the same name used to hold — `current_iteration` never decides *whether* to
stop, only which repair it is.
"""

from __future__ import annotations

import uuid

import pytest

from shared.contracts.dto.engineering_dispatch import (
    EngineeringDispatchOutcome,
    EngineeringDispatchRefusal,
    EngineeringDispatchRepair,
)
from shared.contracts.dto.run import RunStatus, RunType
from shared.models import Run, Task
from src.engineering_dispatch_admission import _prior_attempt

PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
INITIATING_RUN_ID = "live-run-1"


def _todo_task(*, current_iteration: int) -> Task:
    return Task(
        id="task-1",
        project_id=PROJECT_ID,
        story_id="story-1",
        status="todo",
        current_iteration=current_iteration,
    )


def _run(status: RunStatus, *, iteration: int, run_id: str = "eng-first-attempt") -> Run:
    return Run(
        id=run_id,
        project_id=PROJECT_ID,
        type=RunType.ENGINEERING.value,
        status=status.value,
        run_metadata={"triggered_by": "dispatcher", "iteration": iteration},
        result=(
            {"engineering_status": "failed"}
            if status in (RunStatus.COMPLETED, RunStatus.FAILED)
            else None
        ),
    )


@pytest.mark.parametrize("status", [RunStatus.RUNNING, RunStatus.QUEUED])
def test_a_live_attempt_of_an_earlier_iteration_still_stops_the_dispatch(status):
    """Worker alive, supervisor ticked: adopt the attempt, spawn nothing.

    The task is at iteration 1 because the supervisor's retry incremented it;
    the still-live attempt is stamped with iteration 0. Under the old fence that
    mismatch was licence to dispatch. A queued attempt is owned too — the message
    is out and a worker is coming.
    """
    decision = _prior_attempt(
        _todo_task(current_iteration=1), [_run(status, iteration=0)], INITIATING_RUN_ID
    )

    assert decision is not None
    assert decision.outcome is EngineeringDispatchOutcome.REPAIR
    assert decision.repair is EngineeringDispatchRepair.ADOPT_LIVE_ATTEMPT
    assert decision.reason is EngineeringDispatchRefusal.LIVE_ATTEMPT_IN_FLIGHT
    assert decision.run_id == "eng-first-attempt"


def test_a_live_attempt_of_this_iteration_is_this_task_s_own_dispatch():
    """Same action, different event: the message went out, the transition did not."""
    decision = _prior_attempt(
        _todo_task(current_iteration=1), [_run(RunStatus.QUEUED, iteration=1)], INITIATING_RUN_ID
    )

    assert decision.repair is EngineeringDispatchRepair.RECOVER_OWN_ATTEMPT
    assert decision.reason is None


def test_a_closed_attempt_of_an_earlier_iteration_does_not_block():
    """The supervisor closes the attempt before it fails the task, so retries flow.

    Without this the fence would be a deadlock rather than a fix.
    """
    assert (
        _prior_attempt(
            _todo_task(current_iteration=1),
            [_run(RunStatus.FAILED, iteration=0)],
            INITIATING_RUN_ID,
        )
        is None
    )


def test_a_finished_attempt_of_this_iteration_is_replayed_not_redispatched():
    """A worker that finished while the task was stuck in todo owes it its outcome."""
    decision = _prior_attempt(
        _todo_task(current_iteration=0),
        [_run(RunStatus.COMPLETED, iteration=0)],
        INITIATING_RUN_ID,
    )

    assert decision.repair is EngineeringDispatchRepair.REPLAY_FINISHED_RUN
    assert decision.run_id == "eng-first-attempt"

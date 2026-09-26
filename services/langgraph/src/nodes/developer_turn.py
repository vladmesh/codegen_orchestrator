"""What a developer turn is told about the attempts before it.

A story keeps one worker across its tasks, and that worker keeps its CLI
session between turns. Resuming the session is right for a retry of the same
task and wrong for the next task: the conversation still holds the previous
task, and a model that believes it is finished reports that task's commit
again. The story's engineering Runs already record which task each attempt
worked on and which worker ran it, so the next turn is planned from them.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from shared.contracts.dto.run import RunDTO
from shared.contracts.dto.run_result import EngineeringFailureReason, EngineeringRunResult
from shared.contracts.worker_turn import AttemptTurnMetadata

from ..clients.api import LanggraphAPIClient

logger = structlog.get_logger()


@dataclass(frozen=True)
class TurnPlan:
    """How the next turn is sent: a fresh session or not, and what TASK.md opens with."""

    clear_session: bool
    preamble: str


# A taskless attempt (a deploy repair) or one outside a story keeps the turn it
# has always had: there is no task identity to compare and no story history.
UNCHANGED_TURN = TurnPlan(clear_session=False, preamble="")


def format_turn_preamble(
    task_id: str,
    task_title: str,
    *,
    new_task_on_reused_worker: bool,
    previous_attempt_made_no_changes: bool,
) -> str:
    """The section TASK.md opens with, or an empty string when there is nothing to say."""
    sections = []
    if new_task_on_reused_worker:
        sections.append(f"""## This turn's task

This turn is task `{task_id}`: {task_title}. The earlier tasks in this story are
finished: their commits are already on the branch and are not this task's result.
Do not report them, or anything done in an earlier turn, as the result of this task.
Implement this task and commit the change it needs.
""")
    if previous_attempt_made_no_changes:
        sections.append(f"""## The previous attempt made no changes

The previous attempt at task `{task_id}` ({task_title}) finished without changing
anything on the branch, so it was not accepted. Implement this task and commit the
change it needs; a result that reports a commit already on the branch fails again.
""")
    return "\n".join(sections) + ("\n" if sections else "")


def _previous_attempt_made_no_changes(run: RunDTO | None) -> bool:
    return (
        run is not None
        and isinstance(run.result, EngineeringRunResult)
        and run.result.failure_reason is EngineeringFailureReason.NO_NEW_COMMIT
    )


async def plan_turn(state: dict, api_client: LanggraphAPIClient) -> TurnPlan:
    """Decide the session flag and the TASK.md preamble for this attempt's turn.

    - A reused worker whose last turn worked on another task gets a fresh session
      and a preamble naming this task.
    - An attempt whose previous attempt at the same task made no changes gets the
      same, whether the worker is reused or not: it must not resume the turn that
      produced nothing, and it is told why it runs again.
    - A retry of the same task after any other failure keeps its session.
    """
    task_id = state.get("planning_task_id")
    story_id = state.get("story_id")
    if not (task_id and story_id):
        return UNCHANGED_TURN

    attempt_id = state["ownership"].attempt_id
    earlier = [
        run
        for run in await api_client.list_story_engineering_runs(story_id)
        if run.id != attempt_id
    ]
    worker_id = state.get("worker_id")
    new_task_on_reused_worker = False
    if worker_id:
        last_turn = next(
            (
                run
                for run in earlier
                if AttemptTurnMetadata.from_run_metadata(run.run_metadata).worker_id == worker_id
            ),
            None,
        )
        new_task_on_reused_worker = last_turn is None or last_turn.task_id != task_id
    previous_attempt = next((run for run in earlier if run.task_id == task_id), None)
    made_no_changes = _previous_attempt_made_no_changes(previous_attempt)

    if not (new_task_on_reused_worker or made_no_changes):
        return UNCHANGED_TURN

    task = await api_client.get_task(task_id)
    logger.info(
        "developer_turn_starts_fresh",
        task_id=task_id,
        worker_id=worker_id,
        new_task_on_reused_worker=new_task_on_reused_worker,
        previous_attempt_made_no_changes=made_no_changes,
    )
    return TurnPlan(
        clear_session=bool(worker_id),
        preamble=format_turn_preamble(
            task_id,
            task.title,
            new_task_on_reused_worker=new_task_on_reused_worker,
            previous_attempt_made_no_changes=made_no_changes,
        ),
    )

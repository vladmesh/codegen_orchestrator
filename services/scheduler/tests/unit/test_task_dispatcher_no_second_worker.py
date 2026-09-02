"""One story branch, one worker — the dispatcher's half of that guarantee.

The failure this pins down: `supervise_failed_tasks` returns a task to todo and
increments `current_iteration` in the same action. The dispatch fence used to
match a run on that very field, so the moment it was incremented the previous
run — whose worker may still be writing to `story/<id>` — stopped counting as a
reason not to dispatch, and the next tick started a second attempt beside it.

Card 1240 moved that fence into the admission point, where it is decided on
locked rows and answered as a typed decision. Which run stops a dispatch is
pinned there — `services/api/tests/unit/test_engineering_dispatch_prior_attempt.py`
holds the iteration-independence property this file used to hold. What is left
here is the dispatcher's own obligation, and it is exactly as load-bearing: when
the answer says a live attempt owns this task, the tick must publish nothing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID

from _run_routing_factories import _make_task
import pytest

from shared.contracts.dto.engineering_dispatch import (
    EngineeringDispatchOutcome,
    EngineeringDispatchRead,
    EngineeringDispatchRefusal,
    EngineeringDispatchRepair,
)

PROJECT_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def api_client():
    client = AsyncMock()
    client.get_tasks_by_story.return_value = []
    client.get_task_events.return_value = []
    return client


@pytest.fixture
def redis_client():
    client = AsyncMock()
    client.publish_message = AsyncMock()
    client.publish = AsyncMock()
    client.redis = AsyncMock()
    return client


def _todo_task(*, current_iteration: int):
    return _make_task(
        id="task-1",
        project_id=UUID(PROJECT_ID),
        story_id="story-1",
        status="todo",
        current_iteration=current_iteration,
    )


def _live_attempt(repair: EngineeringDispatchRepair) -> EngineeringDispatchRead:
    return EngineeringDispatchRead(
        outcome=EngineeringDispatchOutcome.REPAIR,
        repair=repair,
        reason=(
            EngineeringDispatchRefusal.LIVE_ATTEMPT_IN_FLIGHT
            if repair is EngineeringDispatchRepair.ADOPT_LIVE_ATTEMPT
            else None
        ),
        run_id="eng-first-attempt",
        initiating_run_id="live-run-1",
    )


class TestLiveWorkerSurvivesASupervisorTick:
    @pytest.mark.asyncio
    async def test_no_second_worker_while_the_first_attempt_is_unfinished(
        self, api_client, redis_client
    ):
        """Worker alive, supervisor ticked: the task returns to in_dev, nothing spawned.

        The task is at iteration 1 because the supervisor's retry incremented it,
        and the admission point answered that a live attempt of an earlier
        iteration still owns the branch. Nothing here may second-guess that.
        """
        api_client.get_tasks_by_status.return_value = [_todo_task(current_iteration=1)]
        api_client.admit_engineering_dispatch.return_value = _live_attempt(
            EngineeringDispatchRepair.ADOPT_LIVE_ATTEMPT
        )

        from src.tasks.task_dispatcher import dispatch_todo_tasks

        dispatched = await dispatch_todo_tasks(api_client, redis_client)

        assert dispatched == 0
        redis_client.publish_message.assert_not_called()
        api_client.transition_task.assert_called_once_with("task-1", "in_dev", "dispatcher")

    @pytest.mark.asyncio
    async def test_this_task_s_own_live_attempt_is_completed_not_repeated(
        self, api_client, redis_client
    ):
        """The message is already out; only the transition out of todo was missing."""
        api_client.get_tasks_by_status.return_value = [_todo_task(current_iteration=0)]
        api_client.admit_engineering_dispatch.return_value = _live_attempt(
            EngineeringDispatchRepair.RECOVER_OWN_ATTEMPT
        )

        from src.tasks.task_dispatcher import dispatch_todo_tasks

        dispatched = await dispatch_todo_tasks(api_client, redis_client)

        assert dispatched == 1
        redis_client.publish_message.assert_not_called()
        api_client.transition_task.assert_called_once_with("task-1", "in_dev", "dispatcher")

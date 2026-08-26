"""One story branch, one worker — including across the supervisor's retry tick.

The failure this pins down: `supervise_failed_tasks` returns a task to todo and
increments `current_iteration` in the same action. The dispatch guard used to
match a run on that very field, so the moment it was incremented the previous
run — whose worker may still be writing to `story/<id>` — stopped counting as a
reason not to dispatch, and the next tick started a second attempt beside it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

from _run_routing_factories import _make_project, _make_run, _make_task
import pytest

from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.work_admission import (
    PaidRunStartRead,
    WorkAdmissionOutcome,
    WorkAdmissionRead,
)

PROJECT_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def api_client():
    client = AsyncMock()
    client.get_project.return_value = _make_project(
        id=UUID(PROJECT_ID), config={"workspace_ready": True}
    )
    client.get_tasks_by_story.return_value = []
    client.get_task_events.return_value = []
    client.start_paid_run.return_value = PaidRunStartRead(
        admission=WorkAdmissionRead(outcome=WorkAdmissionOutcome.ADMITTED), run_id="eng-test"
    )
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


def _run(status: RunStatus, *, iteration: int):
    return _make_run(
        id="eng-first-attempt",
        project_id=PROJECT_ID,
        type=RunType.ENGINEERING,
        status=status,
        run_metadata={"triggered_by": "dispatcher", "iteration": iteration},
        created_at=datetime.now(UTC),
        result=(
            {"engineering_status": "failed"}
            if status in (RunStatus.COMPLETED, RunStatus.FAILED)
            else None
        ),
    )


class TestLiveWorkerSurvivesASupervisorTick:
    @pytest.mark.asyncio
    async def test_no_second_worker_while_the_first_attempt_is_unfinished(
        self, api_client, redis_client
    ):
        """Worker alive, supervisor ticked: the task returns to in_dev, nothing is spawned.

        The task is at iteration 1 because the supervisor's retry incremented it;
        the still-running attempt is stamped with iteration 0. Under the old
        guard that mismatch was licence to dispatch.
        """
        api_client.get_tasks_by_status.return_value = [_todo_task(current_iteration=1)]
        api_client.list_runs.return_value = [_run(RunStatus.RUNNING, iteration=0)]

        from src.tasks.task_dispatcher import dispatch_todo_tasks

        dispatched = await dispatch_todo_tasks(api_client, redis_client)

        assert dispatched == 0
        api_client.start_paid_run.assert_not_called()
        redis_client.publish_message.assert_not_called()
        api_client.transition_task.assert_called_once_with("task-1", "in_dev", "dispatcher")

    @pytest.mark.asyncio
    async def test_queued_attempt_of_an_earlier_iteration_also_blocks(
        self, api_client, redis_client
    ):
        """A queued attempt is owned too — the message is out and a worker is coming."""
        api_client.get_tasks_by_status.return_value = [_todo_task(current_iteration=2)]
        api_client.list_runs.return_value = [_run(RunStatus.QUEUED, iteration=0)]

        from src.tasks.task_dispatcher import dispatch_todo_tasks

        dispatched = await dispatch_todo_tasks(api_client, redis_client)

        assert dispatched == 0
        api_client.start_paid_run.assert_not_called()
        redis_client.publish_message.assert_not_called()


class TestGenuineRetryStillDispatches:
    @pytest.mark.asyncio
    async def test_closed_attempt_of_an_earlier_iteration_does_not_block(
        self, api_client, redis_client
    ):
        """The supervisor closes the attempt before it fails the task, so retries flow.

        Without this the guard would be a deadlock rather than a fix.
        """
        api_client.get_tasks_by_status.return_value = [_todo_task(current_iteration=1)]
        api_client.list_runs.return_value = [_run(RunStatus.FAILED, iteration=0)]

        from src.tasks.task_dispatcher import dispatch_todo_tasks

        dispatched = await dispatch_todo_tasks(api_client, redis_client)

        assert dispatched == 1
        api_client.start_paid_run.assert_called_once()
        redis_client.publish_message.assert_called_once()

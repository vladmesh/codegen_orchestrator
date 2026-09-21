"""Real API proof that an operator's resume gives a parked task one fresh attempt.

The governing reproduction is production, 2026-09-17: a story's task sat in
`waiting_human_review` with `current_iteration = max_iterations = 3` after four
attempts failed, a finished failed run for every iteration and no live run. The
documented `POST /tasks/{id}/resume` moved it to in_dev, and within a tick the
supervisor replayed the last failed run onto it and parked it again. No run was
created.

Here the task reaches that state through the pipeline's own ticks — four
admitted attempts, each failed and retried by the supervisor until the retries
are exhausted — and then one operator call must yield a new run, no replayed
outcome, and several quiet supervisor ticks.
"""

import uuid

import httpx
import pytest

from shared.contracts.dto.engineering import EngineeringStatus
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import EngineeringRunResult
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from src.tasks import task_dispatcher
from src.tasks.supervisor import liveness, supervise_failed_tasks, supervise_stuck_tasks
from src.tasks.task_dispatcher import dispatch_todo_tasks

MAX_ITERATIONS = 3


class _RecordingRedis:
    """The engineering and owner queues accept every message."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, object]] = []
        self.owner_events: list[dict] = []

    async def publish_message(self, queue: str, message: object) -> str:
        self.messages.append((queue, message))
        return "1-1"

    async def publish_flat(self, queue: str, fields: dict) -> str:
        self.owner_events.append(fields)
        return "1-1"


async def _story_task(api_client) -> tuple[str, str]:
    telegram_id = uuid.uuid4().int % 1_000_000_000
    created_user = await api_client.request(
        "POST",
        "users/",
        json={"telegram_id": telegram_id, "username": f"operator-resume-{telegram_id}"},
    )
    assert created_user.is_success, created_user.text
    project_id = str(uuid.uuid4())
    created_project = await api_client.request(
        "POST",
        "projects/",
        json={
            "id": project_id,
            "title": "Operator resume after exhausted retries",
            "initiating_run_id": f"init-{uuid.uuid4().hex}",
            "status": "active",
            "config": {"workspace_ready": True},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert created_project.is_success, created_project.text
    created_story = await api_client.request(
        "POST", "stories/", json={"project_id": project_id, "title": "Parked story"}
    )
    assert created_story.is_success, created_story.text
    story_id = created_story.json()["id"]
    task = await api_client.create_task(
        {
            "project_id": project_id,
            "story_id": story_id,
            "type": "feature",
            "title": "Failed for an infrastructure reason since fixed",
            "status": "todo",
            "max_iterations": MAX_ITERATIONS,
        }
    )
    await api_client.transition_story(story_id, "start")
    return story_id, task.id


async def _engineering_runs(api_client, task_id: str) -> list:
    return await api_client.list_runs(task_id=task_id, run_type=RunType.ENGINEERING.value)


async def _tick(api_client, redis) -> None:
    """The dispatcher's pass and the two supervisors that parked the task in production."""
    await dispatch_todo_tasks(api_client, redis)
    await supervise_stuck_tasks(api_client, redis)
    await supervise_failed_tasks(api_client, redis)


async def _fail_live_attempt(api_client, task_id: str) -> None:
    """The worker's attempt ends in failure, as the four production attempts did."""
    live = [
        run
        for run in await _engineering_runs(api_client, task_id)
        if run.status in (RunStatus.QUEUED, RunStatus.RUNNING)
    ]
    assert len(live) == 1, live
    await api_client.update_run(
        live[0].id,
        {
            "status": RunStatus.FAILED.value,
            "error_message": "engineering worker could not be created",
            "result": EngineeringRunResult(engineering_status=EngineeringStatus.FAILED).model_dump(
                mode="json"
            ),
        },
    )


@pytest.mark.asyncio
async def test_resume_after_exhausted_retries_starts_one_fresh_attempt(  # noqa: PLR0915
    api_client, monkeypatch
):
    story_id, task_id = await _story_task(api_client)
    redis = _RecordingRedis()

    # Four attempts, every one failed; the supervisor retries three times and
    # parks the task and its story when the retries run out.
    for _ in range(MAX_ITERATIONS + 1):
        await dispatch_todo_tasks(api_client, redis)
        await _fail_live_attempt(api_client, task_id)
        await supervise_stuck_tasks(api_client, redis)
        await supervise_failed_tasks(api_client, redis)

    task = await api_client.get_task(task_id)
    assert (task.status, task.current_iteration, task.max_iterations) == (
        TaskStatus.WAITING_HUMAN_REVIEW,
        MAX_ITERATIONS,
        MAX_ITERATIONS,
    )
    assert (await api_client.get_story(story_id)).status is StoryStatus.WAITING_HUMAN_REVIEW
    parked_runs = await _engineering_runs(api_client, task_id)
    assert sorted(run.run_metadata["iteration"] for run in parked_runs) == [0, 1, 2, 3]
    assert {run.status for run in parked_runs} == {RunStatus.FAILED}

    resumed = await api_client.request(
        "POST",
        f"tasks/{task_id}/resume",
        json={"guidance": "Worker creation is fixed; try again.", "actor": "admin"},
    )
    assert resumed.is_success, resumed.text
    assert (await api_client.get_story(story_id)).status is StoryStatus.IN_PROGRESS

    try:
        # Both ways an old outcome can be applied to a task: the dispatcher's
        # replay of a finished run (`task_outcome_replayed`) and the supervisor's
        # replay onto an in_dev task with no live run — the one that re-parked it
        # in production. Each is observed, not replaced.
        replayed: list[str] = []
        dispatcher_replay = task_dispatcher._recover_dispatched_task
        supervisor_replay = liveness.replay_terminal_attempt

        async def observed_dispatcher_replay(api, replayed_task_id, run, log):
            replayed.append(run.id)
            await dispatcher_replay(api, replayed_task_id, run, log)

        async def observed_supervisor_replay(api, replayed_task_id, run, actor):
            replayed.append(run.id)
            await supervisor_replay(api, replayed_task_id, run, actor)

        monkeypatch.setattr(task_dispatcher, "_recover_dispatched_task", observed_dispatcher_replay)
        monkeypatch.setattr(liveness, "replay_terminal_attempt", observed_supervisor_replay)
        events_before = len(await api_client.get_task_events(task_id))

        for _ in range(3):
            await _tick(api_client, redis)

        assert replayed == []
        hops = [
            (event.from_status, event.to_status)
            for event in (await api_client.get_task_events(task_id))[events_before:]
            if event.to_status
        ]
        # One hop after the operator's: the dispatcher starting the new attempt.
        assert hops == [(TaskStatus.TODO, TaskStatus.IN_DEV)], hops

        runs = await _engineering_runs(api_client, task_id)
        fresh = [run for run in runs if run.id not in {old.id for old in parked_runs}]
        assert len(fresh) == 1, runs
        assert fresh[0].status is RunStatus.QUEUED
        assert fresh[0].run_metadata["iteration"] == MAX_ITERATIONS + 1
        # Every earlier run is exactly as it was: nothing of them was applied again.
        assert {run.id: run.status for run in runs if run.id != fresh[0].id} == {
            run.id: run.status for run in parked_runs
        }

        task = await api_client.get_task(task_id)
        assert task.status == TaskStatus.IN_DEV
        # The fresh attempt has a retry budget of its own, granted on purpose.
        assert (task.current_iteration, task.max_iterations) == (
            MAX_ITERATIONS + 1,
            MAX_ITERATIONS + 1 + MAX_ITERATIONS,
        )
        assert (await api_client.get_story(story_id)).status is StoryStatus.IN_PROGRESS
        published = [message for _, message in redis.messages if message.task_id == fresh[0].id]
        assert len(published) == 1

        # A second resume while that worker holds the branch is refused with a reason.
        with pytest.raises(httpx.HTTPStatusError) as refused:
            await api_client.request(
                "POST", f"tasks/{task_id}/resume", json={"guidance": "twice", "actor": "admin"}
            )
        assert refused.value.response.status_code == 422, refused.value.response.text  # noqa: PLR2004
        assert refused.value.response.json()["detail"]["reason"] == "task_not_parked"
    finally:
        # The fresh attempt holds a paid-work slot until its run ends; end it so
        # the suites after this one are admitted against an empty stand.
        for run in await _engineering_runs(api_client, task_id):
            if run.status in (RunStatus.QUEUED, RunStatus.RUNNING):
                await _fail_live_attempt(api_client, task_id)

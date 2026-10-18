"""A Product Brief admission wakes the real-Redis task dispatcher long before its backstop."""

import asyncio
import os
import uuid

from _scheduler_wake_service import (
    BACKSTOP_SECONDS,
    WAIT_SECONDS,
    call_api,
    wake_channel_subscribed,
)
import pytest

from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.queues.scheduler_wake import SchedulerWakeTarget
from shared.queues import ENGINEERING_QUEUE
from shared.redis import RedisStreamClient

_REQUIREMENT = {
    "id": "r1",
    "text": "It stores a book",
    "user_wording": "I want it to remember the books I read",
    "wording_reference": None,
    "user_facing": True,
}
_CONTENT = {
    "summary": "A bot that tracks reading",
    "must_requirements": [_REQUIREMENT],
    "initial_settings": [],
    "language": "en",
    "usage_examples": [
        {"requirement_id": "r1", "user_sends": "the text Dune", "product_answers": "Saved Dune"}
    ],
    "limitations": [],
    "variant_choices": [],
}


class _RecordingRedis:
    """The dispatcher's queues accept every message; the wake channel stays on real Redis."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, object]] = []

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def publish_message(self, queue: str, message: object) -> str:
        self.messages.append((queue, message))
        return "1-1"

    async def publish_flat(self, queue: str, fields: dict) -> str:
        return "1-1"

    def engineering_task_ids(self) -> list[str]:
        return [m.planning_task_id for queue, m in self.messages if queue == ENGINEERING_QUEUE]


async def _planned_brief_task(api_client) -> tuple[str, str, str]:
    """A brief whose plan covers its one requirement with one unreleased Task."""
    telegram_id = uuid.uuid4().int % 1_000_000_000
    await call_api(
        api_client,
        "POST",
        "users/",
        json={"telegram_id": telegram_id, "username": f"dispatch-wake-{telegram_id}"},
    )
    project_id = str(uuid.uuid4())
    await call_api(
        api_client,
        "POST",
        "projects/",
        json={
            "id": project_id,
            "title": "Dispatcher wake",
            "initiating_run_id": f"init-{uuid.uuid4().hex}",
            "status": "active",
            "config": {"workspace_ready": True},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    story = await call_api(
        api_client, "POST", "stories/", json={"project_id": project_id, "title": "Wake"}
    )
    await call_api(api_client, "POST", f"stories/{story['id']}/start", json={"actor": "test"})
    brief = await call_api(
        api_client,
        "POST",
        "product-briefs/",
        json={
            "project_id": project_id,
            "title": "Reading tracker",
            "content": _CONTENT,
            "request_id": f"req-{uuid.uuid4().hex}",
        },
    )
    await call_api(
        api_client,
        "POST",
        f"product-briefs/{brief['id']}/confirm",
        json={"request_id": f"conf-{uuid.uuid4().hex}", "content": _CONTENT},
    )
    await call_api(
        api_client, "POST", f"product-briefs/{brief['id']}/story", json={"story_id": story["id"]}
    )
    claim = await call_api(
        api_client, "POST", f"product-briefs/{brief['id']}/planning-attempts/claim"
    )
    attempt = claim["planning_attempt_id"]
    task = await call_api(
        api_client,
        "POST",
        "tasks/",
        json={
            "project_id": project_id,
            "type": "feature",
            "title": "Planned",
            "status": "todo",
            "story_id": story["id"],
            "planning_attempt_id": attempt,
        },
    )
    assert task["dispatch_admitted"] is False
    await call_api(
        api_client,
        "PUT",
        f"product-briefs/{brief['id']}/coverage/r1",
        json={"requirement_id": "r1", "planning_attempt_id": attempt, "task_id": task["id"]},
    )
    return brief["id"], attempt, task["id"]


async def _settle_dispatched(api_client, task_id: str) -> None:
    """Take the Task out of dispatch and cancel its paid Run, so later suites keep their slot."""
    for run in await api_client.list_runs(task_id=task_id, run_type=RunType.ENGINEERING.value):
        if run.status in (RunStatus.QUEUED, RunStatus.RUNNING):
            await api_client.update_run(run.id, {"status": RunStatus.CANCELLED.value})
    task = await call_api(api_client, "GET", f"tasks/{task_id}")
    if task["status"] not in ("done", "cancelled"):
        await call_api(api_client, "DELETE", f"tasks/{task_id}")


@pytest.mark.asyncio
async def test_a_brief_admission_dispatches_its_released_task_before_the_backstop(
    api_client, monkeypatch
):
    from src.tasks import scheduler_wake_listener, task_dispatcher

    queues = _RecordingRedis()
    monkeypatch.setattr(task_dispatcher, "RedisStreamClient", lambda: queues)
    monkeypatch.setattr(task_dispatcher, "_dispatch_interval", lambda: BACKSTOP_SECONDS)
    monkeypatch.setattr(scheduler_wake_listener, "_reconnect_interval", lambda: BACKSTOP_SECONDS)
    cycles: asyncio.Queue[int] = asyncio.Queue()
    dispatch_todo_tasks = task_dispatcher.dispatch_todo_tasks

    async def observed_dispatch(api, redis_client):
        dispatched = await dispatch_todo_tasks(api, redis_client)
        cycles.put_nowait(dispatched)
        return dispatched

    monkeypatch.setattr(task_dispatcher, "dispatch_todo_tasks", observed_dispatch)
    brief_id, attempt, task_id = await _planned_brief_task(api_client)

    wakes = {target: asyncio.Event() for target in SchedulerWakeTarget}
    observer = RedisStreamClient(os.environ["REDIS_URL"])
    await observer.connect()
    listener = asyncio.create_task(scheduler_wake_listener.scheduler_wake_listener(wakes))
    loop = None
    try:
        await wake_channel_subscribed(observer)
        loop = asyncio.create_task(
            task_dispatcher.task_dispatcher_loop(wake=wakes[SchedulerWakeTarget.TASK_DISPATCHER])
        )
        await asyncio.wait_for(cycles.get(), WAIT_SECONDS)
        assert task_id not in queues.engineering_task_ids()
        assert (await call_api(api_client, "GET", f"tasks/{task_id}"))["status"] == "todo"

        admitted = await call_api(
            api_client,
            "POST",
            f"product-briefs/{brief_id}/admit",
            json={"planning_attempt_id": attempt},
        )
        assert admitted["released_task_ids"] == [task_id]

        await asyncio.wait_for(cycles.get(), WAIT_SECONDS)

        assert queues.engineering_task_ids().count(task_id) == 1
        task = await call_api(api_client, "GET", f"tasks/{task_id}")
        assert task["dispatch_admitted"] is True
        assert task["status"] == "in_dev"
        assert not wakes[SchedulerWakeTarget.STORY_COMPLETION].is_set()
    finally:
        tasks = [task for task in (loop, listener) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await observer.close()
        await _settle_dispatched(api_client, task_id)

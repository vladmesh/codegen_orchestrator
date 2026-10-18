"""An API done transition wakes the real-Redis completion sweep long before its backstop."""

import asyncio
import os
import uuid

import pytest

from shared.contracts.queues.scheduler_wake import SCHEDULER_WAKE_CHANNEL, SchedulerWakeTarget
from shared.redis import RedisStreamClient

BACKSTOP_SECONDS = 3600
WAIT_SECONDS = 15
BRANCH_SHA = "0123456789abcdef0123456789abcdef01234567"


class _GitHub:
    """Answers for one repository only, so other stories in the shared database stay put."""

    def __init__(self, repo_name: str) -> None:
        self.repo_name = repo_name
        self.pull_requests: list[str] = []

    def __call__(self) -> "_GitHub":
        return self

    async def __aenter__(self) -> "_GitHub":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def get_ref_sha(self, owner: str, repo_name: str, ref: str) -> str:
        if repo_name != self.repo_name:
            raise RuntimeError("repository outside this test")
        return BRANCH_SHA

    async def create_pull_request(self, owner, repo_name, *, head, base, title, body) -> dict:
        self.pull_requests.append(head)
        return {"number": 7, "node_id": "PR_wake", "head": {"ref": head, "sha": BRANCH_SHA}}


async def _call(api_client, method: str, path: str, **kwargs) -> dict:
    response = await api_client.request(method, path, **kwargs)
    assert response.is_success, response.text
    return response.json()


async def _story_with_task_in_dev(api_client, repo_name: str) -> tuple[str, str]:
    telegram_id = uuid.uuid4().int % 1_000_000_000
    await _call(
        api_client,
        "POST",
        "users/",
        json={"telegram_id": telegram_id, "username": f"wake-{telegram_id}"},
    )
    project_id = str(uuid.uuid4())
    await _call(
        api_client,
        "POST",
        "projects/",
        json={
            "id": project_id,
            "title": "Completion wake",
            "initiating_run_id": f"init-{uuid.uuid4().hex}",
            "config": {},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    await _call(
        api_client,
        "POST",
        "repositories/",
        json={
            "project_id": project_id,
            "name": repo_name,
            "git_url": f"https://github.com/test-org/{repo_name}",
        },
    )
    story = await _call(
        api_client, "POST", "stories/", json={"project_id": project_id, "title": "Wake"}
    )
    task = await _call(
        api_client,
        "POST",
        "tasks/",
        json={
            "project_id": project_id,
            "story_id": story["id"],
            "title": "Engineering",
            "status": "todo",
        },
    )
    await _call(api_client, "POST", f"stories/{story['id']}/start", json={"actor": "test"})
    await _call(
        api_client,
        "POST",
        f"tasks/{task['id']}/transition",
        params={"to_status": "in_dev"},
        json={"actor": "test"},
    )
    return story["id"], task["id"]


async def _subscribed(redis_client: RedisStreamClient) -> None:
    async with asyncio.timeout(WAIT_SECONDS):
        while True:
            [(_channel, count)] = await redis_client.redis.pubsub_numsub(SCHEDULER_WAKE_CHANNEL)
            if count:
                return
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_an_api_done_transition_sweeps_its_story_before_the_backstop(api_client, monkeypatch):
    from src.tasks import scheduler_wake_listener, story_completion, story_completion_loop

    repo_name = f"wake-{uuid.uuid4().hex[:10]}"
    github = _GitHub(repo_name)
    monkeypatch.setattr(story_completion, "GitHubAppClient", github)
    monkeypatch.setattr(
        story_completion_loop, "_story_completion_interval", lambda: BACKSTOP_SECONDS
    )
    monkeypatch.setattr(scheduler_wake_listener, "_reconnect_interval", lambda: BACKSTOP_SECONDS)
    cycles: asyncio.Queue[int] = asyncio.Queue()

    async def observed_cycle(api, redis_client):
        completed = await story_completion.complete_stories(api, redis_client)
        cycles.put_nowait(completed)
        return completed

    monkeypatch.setattr(story_completion_loop, "complete_stories", observed_cycle)
    story_id, task_id = await _story_with_task_in_dev(api_client, repo_name)

    wakes = {target: asyncio.Event() for target in SchedulerWakeTarget}
    wake = wakes[SchedulerWakeTarget.STORY_COMPLETION]
    observer = RedisStreamClient(os.environ["REDIS_URL"])
    await observer.connect()
    listener = asyncio.create_task(scheduler_wake_listener.scheduler_wake_listener(wakes))
    loop = None
    try:
        await _subscribed(observer)
        loop = asyncio.create_task(story_completion_loop.story_completion_loop(wake=wake))
        await asyncio.wait_for(cycles.get(), WAIT_SECONDS)
        assert (await _call(api_client, "GET", f"stories/{story_id}"))["status"] == "in_progress"

        for hop in ("in_ci", "testing", "done"):
            await _call(
                api_client,
                "POST",
                f"tasks/{task_id}/transition",
                params={"to_status": hop},
                json={"actor": "engineering-worker"},
            )

        assert await asyncio.wait_for(cycles.get(), WAIT_SECONDS) == 1

        story = await _call(api_client, "GET", f"stories/{story_id}")
        assert story["status"] == "pr_review"
        assert story["pr_number"] == 7
        assert github.pull_requests == [f"story/{story_id}"]
    finally:
        tasks = [task for task in (loop, listener) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await observer.close()

"""Real API calls and native wake-channel readiness shared by the scheduler wake service tests."""

from __future__ import annotations

import asyncio
import uuid

from shared.contracts.queues.scheduler_wake import SCHEDULER_WAKE_CHANNEL
from shared.redis import RedisStreamClient

#: A loop interval no test waits out, so only a wake can run the next cycle in time.
BACKSTOP_SECONDS = 3600
WAIT_SECONDS = 15


async def call_api(api_client, method: str, path: str, **kwargs) -> dict:
    """One successful real API request, as its JSON body."""
    response = await api_client.request(method, path, **kwargs)
    assert response.is_success, response.text
    return response.json()


async def story_in_repository(api_client, repo_name: str, title: str) -> tuple[str, str]:
    """A new user's project with one repository `test-org/<repo_name>` and one new Story.

    Returns the project and Story ids.
    """
    telegram_id = uuid.uuid4().int % 1_000_000_000
    await call_api(
        api_client,
        "POST",
        "users/",
        json={"telegram_id": telegram_id, "username": f"wake-{telegram_id}"},
    )
    project_id = str(uuid.uuid4())
    await call_api(
        api_client,
        "POST",
        "projects/",
        json={
            "id": project_id,
            "title": title,
            "initiating_run_id": f"init-{uuid.uuid4().hex}",
            "config": {},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    await call_api(
        api_client,
        "POST",
        "repositories/",
        json={
            "project_id": project_id,
            "name": repo_name,
            "git_url": f"https://github.com/test-org/{repo_name}",
        },
    )
    story = await call_api(
        api_client, "POST", "stories/", json={"project_id": project_id, "title": "Wake"}
    )
    return project_id, story["id"]


async def wake_channel_subscribed(redis_client: RedisStreamClient) -> None:
    """Return once a listener is subscribed to `scheduler:wake`, within `WAIT_SECONDS`."""
    async with asyncio.timeout(WAIT_SECONDS):
        while True:
            [(_channel, count)] = await redis_client.redis.pubsub_numsub(SCHEDULER_WAKE_CHANNEL)
            if count:
                return
            await asyncio.sleep(0.05)

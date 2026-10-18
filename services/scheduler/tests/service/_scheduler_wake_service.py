"""Real API calls and native wake-channel readiness shared by the scheduler wake service tests."""

from __future__ import annotations

import asyncio

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


async def wake_channel_subscribed(redis_client: RedisStreamClient) -> None:
    """Return once a listener is subscribed to `scheduler:wake`, within `WAIT_SECONDS`."""
    async with asyncio.timeout(WAIT_SECONDS):
        while True:
            [(_channel, count)] = await redis_client.redis.pubsub_numsub(SCHEDULER_WAKE_CHANNEL)
            if count:
                return
            await asyncio.sleep(0.05)

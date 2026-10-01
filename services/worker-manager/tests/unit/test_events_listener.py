import asyncio
from unittest.mock import AsyncMock, patch

from fakeredis import aioredis
import pytest

from src.events import DockerEventsListener


@pytest.mark.asyncio
async def test_docker_events_listener_reconnects_after_stream_failure():
    listener = DockerEventsListener(aioredis.FakeRedis(decode_responses=True))
    listener._listen_once = AsyncMock(
        side_effect=[RuntimeError("docker stream reset"), asyncio.CancelledError()]
    )

    with patch("src.events.asyncio.sleep", new_callable=AsyncMock) as sleep:
        await listener.start()

    assert listener._listen_once.await_count == 2
    sleep.assert_awaited_once_with(1.0)

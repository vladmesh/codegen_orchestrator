from unittest.mock import patch

from fakeredis import aioredis
import pytest_asyncio

from shared.redis.client import RedisStreamClient


@pytest_asyncio.fixture
async def redis_client():
    """Returns a RedisStreamClient backed by fakeredis."""
    fake_redis = aioredis.FakeRedis(decode_responses=True)

    # Patch redis.asyncio.from_url to return our fake instance
    with patch("redis.asyncio.from_url", return_value=fake_redis):
        client = RedisStreamClient("redis://fake")
        await client.connect()
        yield client
        await client.close()
        await fake_redis.close()

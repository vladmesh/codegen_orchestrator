"""Service test fixtures for Telegram Bot."""

import os

import pytest_asyncio
from redis.asyncio import Redis


@pytest_asyncio.fixture
async def redis_client():
    """Real Redis connection for service tests."""
    url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    client = Redis.from_url(url, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()

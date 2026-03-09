"""Service test fixtures — real Redis, no mocks on infrastructure."""

from __future__ import annotations

import os

import pytest
import redis.asyncio as aioredis


@pytest.fixture
async def real_redis():
    """Real async Redis client from REDIS_URL env var."""
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    client = aioredis.from_url(url, decode_responses=False)
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
async def _clean_redis(real_redis):
    """Clean up test keys before and after each test."""
    keys_to_clean = ["story:workers", "worker:commands"]
    for key in keys_to_clean:
        await real_redis.delete(key)
    yield
    for key in keys_to_clean:
        await real_redis.delete(key)

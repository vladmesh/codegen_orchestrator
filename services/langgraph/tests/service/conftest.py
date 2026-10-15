"""Service test fixtures — real Redis, no mocks on infrastructure."""

from __future__ import annotations

import os

import pytest
import redis.asyncio as aioredis

from shared.queues import STORY_WORKERS_KEY, WORKER_COMMANDS


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
    keys_to_clean = [STORY_WORKERS_KEY, WORKER_COMMANDS]
    for key in keys_to_clean:
        await real_redis.delete(key)
    yield
    for key in keys_to_clean:
        await real_redis.delete(key)


@pytest.fixture
async def deploy_worker_config():
    """The deploy worker's process system config, started from the seeded test API."""
    from src.config.system_config import start_system_config, stop_system_config
    from src.consumers.deploy import DEPLOY_CONFIG_CACHE_TTL_SECONDS

    store = await start_system_config(
        os.environ["TEST_API_BASE_URL"], cache_ttl=DEPLOY_CONFIG_CACHE_TTL_SECONDS
    )
    store.validate_required(["deploy.deploy_lock_ttl"])
    yield
    await stop_system_config()

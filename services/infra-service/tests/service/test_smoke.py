"""Service smoke tests for infra-service compose stack."""

import os

import pytest
import redis.asyncio as redis


@pytest.mark.asyncio
async def test_service_redis_smoke():
    client = redis.from_url(os.environ["REDIS_URL"])
    try:
        assert await client.ping()
    finally:
        await client.aclose()

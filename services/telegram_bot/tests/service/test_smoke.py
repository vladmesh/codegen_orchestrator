"""Service smoke tests for telegram-bot compose stack."""

import pytest


@pytest.mark.asyncio
async def test_service_redis_smoke(redis_client):
    assert await redis_client.ping()

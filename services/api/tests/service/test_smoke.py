import pytest
from redis.asyncio import Redis
from sqlalchemy import text


@pytest.mark.asyncio
async def test_service_db_smoke(db_session):
    """Verify database connection works."""
    result = await db_session.execute(text("SELECT 1"))
    assert result.scalar() == 1


@pytest.mark.asyncio
async def test_service_redis_smoke(redis_client: Redis):
    """Verify Redis connection works."""
    await redis_client.set("smoke_test", "passed")
    value = await redis_client.get("smoke_test")
    assert value == b"passed"

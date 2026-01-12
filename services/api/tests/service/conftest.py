import asyncio
from collections.abc import AsyncGenerator
import os

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Use env vars or defaults matching docker-compose
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@db:5432/postgres")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for each test case."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def db_engine():
    engine = create_async_engine(DATABASE_URL, echo=False)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="function")
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:
    async_session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session
        await session.rollback()


@pytest.fixture(scope="function")
async def redis_client() -> AsyncGenerator[Redis, None]:
    client = Redis.from_url(REDIS_URL)
    yield client
    await client.close()

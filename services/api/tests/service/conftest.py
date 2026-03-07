from collections.abc import AsyncGenerator
import os
import sys

# Ensure /app is in path so 'src' can be imported
sys.path.append("/app")

from httpx import ASGITransport, AsyncClient
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Use env vars or defaults matching docker-compose
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@db:5432/postgres")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")


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


@pytest.fixture(scope="function")
async def async_client() -> AsyncGenerator[AsyncClient, None]:
    from src.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


TASK_TEST_TELEGRAM_ID = 999000999
TASK_TEST_PROJECT_ID = "test-tasks-proj"


@pytest.fixture(scope="session")
async def _tasks_project():
    """Create a user + project once per test session for task tests."""
    from src.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Ensure user exists
        resp = await client.get(f"/api/users/by-telegram/{TASK_TEST_TELEGRAM_ID}")
        if resp.status_code == 404:
            await client.post(
                "/api/users/",
                json={
                    "telegram_id": TASK_TEST_TELEGRAM_ID,
                    "username": "test_task",
                    "first_name": "Test",
                    "is_admin": True,
                },
            )

        # Ensure project exists
        resp = await client.get(f"/api/projects/{TASK_TEST_PROJECT_ID}")
        if resp.status_code == 404:
            await client.post(
                "/api/projects/",
                json={
                    "id": TASK_TEST_PROJECT_ID,
                    "name": "Test Tasks Project",
                    "status": "active",
                    "config": {},
                },
                headers={"X-Telegram-ID": str(TASK_TEST_TELEGRAM_ID)},
            )

    return TASK_TEST_PROJECT_ID

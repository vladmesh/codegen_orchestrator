"""Common pytest fixtures for backend API tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
import os
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy.engine import Connection
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import ConnectionPoolEntry

# Ensure the backend uses a lightweight SQLite database for tests.
# Use /tmp to avoid PermissionError when running as non-root inside Docker.

_test_tmp = Path(tempfile.gettempdir()) / "backend_tests"
_test_tmp.mkdir(parents=True, exist_ok=True)
TEST_DB_PATH = _test_tmp / "test.db"
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{TEST_DB_PATH}")
os.environ.setdefault("ASYNC_DATABASE_URL", f"sqlite+aiosqlite:///{TEST_DB_PATH}")

# Set required environment variables for tests
os.environ.setdefault("APP_NAME", "test-backend")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")
os.environ.setdefault("USERS_GRANT_CAPABILITY", "test-grant-capability")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ.setdefault("POSTGRES_DB", "test_db")
os.environ.setdefault("POSTGRES_USER", "test_user")
os.environ.setdefault("POSTGRES_PASSWORD", "test_password")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

from services.backend.src.core.settings import get_settings  # noqa: E402

get_settings.cache_clear()
TEST_DB_PATH.unlink(missing_ok=True)

from services.backend.src.app.factory import create_app  # noqa: E402
import services.backend.src.app.models.registry  # noqa: E402, F401  (register models for metadata)
from services.backend.src.core.db import get_async_db  # noqa: E402  (after env setup)
from services.backend.src.core.orm import Base  # noqa: E402


@pytest_asyncio.fixture(scope="session")
async def db_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Create database schema once per test session."""
    from sqlalchemy import event

    engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB_PATH}", echo=False)

    # SQLite requires explicit transaction management for savepoints to work.
    # Without this, pysqlite intercepts BEGIN/COMMIT and breaks rollback isolation.
    # See: https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#serializable-isolation-savepoints-transactional-ddl
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(
        dbapi_conn: DBAPIConnection, connection_record: ConnectionPoolEntry
    ) -> None:
        dbapi_conn.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _do_begin(conn: Connection) -> None:
        conn.exec_driver_sql("BEGIN")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest_asyncio.fixture()
async def db_session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional database session for each test."""

    connection = await db_engine.connect()
    transaction = await connection.begin()
    session_maker = async_sessionmaker(bind=connection, class_=AsyncSession, expire_on_commit=False)
    session = session_maker()
    try:
        yield session
    finally:
        await session.close()
        if transaction.is_active:
            await transaction.rollback()
        await connection.close()


@pytest_asyncio.fixture()
async def app(db_session: AsyncSession) -> AsyncGenerator[FastAPI, None]:
    """Return a FastAPI app with the test database wired in."""

    application = create_app()

    import shared.generated.events as events_module

    # Mock broker to avoid requiring a live Redis instance in unit tests
    mock_broker = AsyncMock()
    original_broker = events_module._broker
    events_module._broker = mock_broker

    async def _get_test_db() -> AsyncGenerator[AsyncSession, None]:
        async with db_session.begin_nested():
            yield db_session

    application.dependency_overrides[get_async_db] = _get_test_db
    try:
        yield application
    finally:
        application.dependency_overrides.clear()
        events_module._broker = original_broker
        # Reset cached publishers so they don't leak mock references
        for attr in list(vars(events_module)):
            if attr.startswith("_pub_"):
                setattr(events_module, attr, None)


@pytest_asyncio.fixture()
async def client(app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    """Return an async HTTP client backed by the configured app."""

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as test_client:
        yield test_client


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_db() -> Generator[None, None, None]:
    """Ensure the temporary SQLite database file is removed after tests."""

    yield
    TEST_DB_PATH.unlink(missing_ok=True)

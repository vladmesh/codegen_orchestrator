"""Database engine and session management."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .orm import Base, CreatedAtMixin, ORMBase, TzAwareDateTime  # noqa: F401
from .settings import get_settings

settings = get_settings()
async_engine = create_async_engine(settings.async_database_url, future=True, echo=False)
AsyncSessionLocal = async_sessionmaker(
    bind=async_engine, autoflush=False, autocommit=False, class_=AsyncSession
)


async def get_async_db() -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional scope around a series of async operations.

    Automatically commits transactions on successful request completion
    and rolls back on exceptions.
    """

    db = AsyncSessionLocal()
    try:
        yield db
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()

"""FastAPI dependencies for authorization and shared resources."""

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import User
from shared.redis.client import RedisStreamClient

from .config import get_settings
from .database import get_async_session

# ---------------------------------------------------------------------------
# Redis client singleton
# ---------------------------------------------------------------------------

_redis_client: RedisStreamClient | None = None


async def init_redis() -> None:
    """Initialize the Redis client singleton. Call during app startup."""
    global _redis_client  # noqa: PLW0603
    settings = get_settings()
    _redis_client = RedisStreamClient(redis_url=settings.redis_url)
    await _redis_client.connect()


async def close_redis() -> None:
    """Close the Redis client. Call during app shutdown."""
    global _redis_client  # noqa: PLW0603
    if _redis_client is not None:
        await _redis_client.close()
        _redis_client = None


def get_redis_client() -> RedisStreamClient:
    """FastAPI dependency — returns the Redis client singleton."""
    if _redis_client is None:
        raise RuntimeError("Redis client not initialized. Call init_redis() during app startup.")
    return _redis_client


async def get_current_user(
    x_telegram_id: int = Header(..., alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
) -> User:
    """Get current user from X-Telegram-ID header.

    Raises 422 if header missing, 404 if user not found.
    """
    query = select(User).where(User.telegram_id == x_telegram_id)
    result = await db.execute(query)
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with telegram_id {x_telegram_id} not found",
        )
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """Require user to be admin.

    Raises 403 if user is not admin.
    """
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user

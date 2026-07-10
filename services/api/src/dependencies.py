"""FastAPI dependencies for authorization and shared resources."""

import datetime as dt
import secrets

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
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


async def is_internal_service(
    x_internal_key: str | None = Header(None, alias="X-Internal-Key"),
) -> bool:
    """Return True when the request carries a valid internal service token."""
    if x_internal_key is None:
        return False
    return secrets.compare_digest(x_internal_key, get_settings().internal_api_key)


async def require_internal_or_admin(
    _is_internal: bool = Depends(is_internal_service),
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
) -> None:
    """Allow internal services or admin users."""
    if _is_internal:
        return
    if x_telegram_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    query = select(User).where(User.telegram_id == x_telegram_id)
    result = await db.execute(query)
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with telegram_id {x_telegram_id} not found",
        )
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")


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


# ---------------------------------------------------------------------------
# Raw Redis (key-value access for LK tokens)
# ---------------------------------------------------------------------------

_bearer_scheme = HTTPBearer()

LK_JWT_ALGORITHM = "HS256"
LK_JWT_TTL = dt.timedelta(hours=24)


def get_raw_redis():
    """Return the underlying redis.asyncio.Redis instance for key-value ops."""
    client = get_redis_client()
    return client.redis


def create_lk_jwt(user_id: int) -> str:
    """Create a JWT for LK user with 24h TTL."""
    settings = get_settings()
    payload = {
        "sub": str(user_id),
        "exp": dt.datetime.now(dt.UTC) + LK_JWT_TTL,
        "iat": dt.datetime.now(dt.UTC),
    }
    return jwt.encode(payload, settings.lk_jwt_secret, algorithm=LK_JWT_ALGORITHM)


async def get_lk_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_async_session),
) -> User:
    """Decode LK JWT and return the authenticated user.

    Raises 401 if token is invalid, expired, or user not found.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.lk_jwt_secret,
            algorithms=[LK_JWT_ALGORITHM],
        )
        user_id = int(payload["sub"])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, KeyError, ValueError) as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        ) from e

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    return user

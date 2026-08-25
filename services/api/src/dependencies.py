"""FastAPI dependencies for authorization and shared resources."""

import datetime as dt
import secrets

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import User
from shared.redis.client import RedisStreamClient

from .config import get_settings
from .database import get_async_session

_optional_bearer_scheme = HTTPBearer(auto_error=False)

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


async def resolve_actor(
    *,
    is_internal: bool,
    telegram_id: int | None,
    credentials: HTTPAuthorizationCredentials | None = None,
    db: AsyncSession,
) -> User | None:
    """Who is acting on this request? This is the only place that decides.

    `None` means a service acting for itself: a valid `X-Internal-Key` and no user
    named. Anything else is the named user, and that user's own rights decide what
    the request may reach — the key authenticates the caller, it does not make it
    anyone's deputy. Every guard that takes the internal flag asks this function
    rather than reading the flag itself, so the rule cannot be half-applied the way
    it was when `projects.py` enforced it and `runs.py` did not.

    Raises 401 when nobody is identified at all, and 404 when the named user is
    unknown to us.
    """
    if credentials is not None and not is_internal:
        bearer_user = await get_lk_user(credentials=credentials, db=db)
        if telegram_id is not None and telegram_id != bearer_user.telegram_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Actor mismatch")
        return bearer_user

    if telegram_id is None:
        if is_internal:
            return None
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with telegram_id {telegram_id} not found",
        )
    return user


async def require_internal_or_admin(
    _is_internal: bool = Depends(is_internal_service),
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
    db: AsyncSession = Depends(get_async_session),
) -> None:
    """Allow internal services acting for themselves, and admin users."""
    actor = await resolve_actor(
        is_internal=_is_internal,
        telegram_id=x_telegram_id,
        credentials=credentials,
        db=db,
    )
    if actor is None:
        return
    if not actor.is_admin:
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

# The gate below has to tell "no token" from "bad token", so it reads the header
# without turning a missing one into an error of its own.

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


# ---------------------------------------------------------------------------
# The application-wide gate
# ---------------------------------------------------------------------------

# The complete list of routes that answer without a credential. Everything the
# app serves is closed by `require_authenticated_caller`, which is installed once
# on the FastAPI instance, so a router added tomorrow is shut by default and a new
# anonymous route can only be opened by adding a line here — deliberately, with a
# reason. Every entry needs a comment saying why it must be anonymous.
ANONYMOUS_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        # Service banner: name and version of the API, no data behind it.
        ("GET", "/"),
        # Liveness probe. Compose healthchecks and CI wait on this before any
        # credential exists in the environment they run in.
        ("GET", "/health"),
        # The LK token exchange mints the dashboard's first JWT, so by definition
        # its caller has nothing to authenticate with yet. The one-time token in
        # the body is the secret, and the handler verifies it against Redis.
        ("POST", "/api/lk/auth/token"),
    }
)


async def require_authenticated_caller(
    request: Request,
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
    db: AsyncSession = Depends(get_async_session),
) -> None:
    """No handler runs for a caller we cannot name. One gate, whole application.

    Two credentials get through: a valid `X-Internal-Key`, which every service
    sends by construction (`shared/clients/internal_api.py` puts it on every
    request), and an LK bearer token. What does *not* get through is
    `X-Telegram-ID`. That header names a user, it never proved one, and anything
    that can reach the API's port can send it — which is how a worker container
    could `POST /api/users` itself an administrator. Guards downstream still read
    the header, but only after this gate has established that the caller is
    entitled to name a user at all.

    Enforcement lives here and nowhere else on purpose: a router included without
    a `dependencies=` of its own is still closed, and the test parametrized over
    `app.routes` fails the moment that stops being true.
    """
    if (request.method, request.url.path) in ANONYMOUS_ROUTES:
        return
    if _is_internal:
        return
    if credentials is not None:
        # Raises 401 for an invalid, expired or orphaned token.
        await get_lk_user(credentials=credentials, db=db)
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
    )

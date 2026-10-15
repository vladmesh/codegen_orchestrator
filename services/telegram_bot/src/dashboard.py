"""One-time links into the user dashboard."""

import uuid

import redis.asyncio as redis_lib

from .clients.api import api_client
from .config import get_settings

# The API's `/lk/auth/token` exchange reads and deletes this key exactly once.
LK_TOKEN_PREFIX = "lk_token:"  # noqa: S105 - Redis key prefix, not a password
LK_TOKEN_TTL_SECONDS = 300


async def mint_dashboard_url(redis: redis_lib.Redis, telegram_id: int) -> str | None:
    """Store a one-time token for `telegram_id` and return its dashboard URL.

    None when the user has no projects: there is nothing for the dashboard to show.
    """
    if not await api_client.list_projects(telegram_id):
        return None
    token = str(uuid.uuid4())
    await redis.set(f"{LK_TOKEN_PREFIX}{token}", str(telegram_id), ex=LK_TOKEN_TTL_SECONDS)
    return f"{get_settings().lk_domain}/auth?token={token}"

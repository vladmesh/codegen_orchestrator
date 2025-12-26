"""API service configuration.

Requires: DATABASE_URL, REDIS_URL
Optional: TELEGRAM_BOT_TOKEN (for notifications)
"""

from functools import lru_cache

from shared.config import (
    BaseSettings,
    database_url_field,
    redis_url_field,
    telegram_token_field,
)


class Settings(BaseSettings):
    """API service settings."""

    # Required
    database_url: str = database_url_field(required=True)
    redis_url: str = redis_url_field(required=True)

    # Optional - notifications work without token in dev
    telegram_bot_token: str = telegram_token_field(required=False)


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance.

    Validates required env vars on first call.
    Raises ValidationError if DATABASE_URL or REDIS_URL are missing.
    """
    return Settings()

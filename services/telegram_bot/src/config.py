"""Telegram Bot service configuration.

Requires: REDIS_URL, API_URL, TELEGRAM_BOT_TOKEN
"""

from functools import lru_cache

from shared.config import (
    BaseSettings,
    api_url_field,
    redis_url_field,
    telegram_token_field,
)


class Settings(BaseSettings):
    """Telegram Bot service settings."""

    # Required
    redis_url: str = redis_url_field(required=True)
    api_url: str = api_url_field(required=True)
    telegram_bot_token: str = telegram_token_field(required=True)


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance.

    Validates required env vars on first call.
    Raises ValidationError if any required var is missing.
    """
    return Settings()

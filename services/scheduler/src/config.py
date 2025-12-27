"""Scheduler service configuration.

Requires: DATABASE_URL, REDIS_URL, API_BASE_URL
"""

from functools import lru_cache

from shared.config import BaseSettings, api_base_url_field, database_url_field, redis_url_field


class Settings(BaseSettings):
    """Scheduler service settings."""

    # Required
    database_url: str = database_url_field(required=True)
    redis_url: str = redis_url_field(required=True)
    api_base_url: str = api_base_url_field(required=True)


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance.

    Validates required env vars on first call.
    Raises ValidationError if any required var is missing.
    """
    return Settings()

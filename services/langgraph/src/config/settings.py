"""LangGraph service configuration.

Requires: REDIS_URL, API_URL
Does NOT need DATABASE_URL (accesses DB via API)
"""

from functools import lru_cache

from shared.config import (
    BaseSettings,
    api_url_field,
    redis_url_field,
)


class Settings(BaseSettings):
    """LangGraph service settings."""

    # Required
    redis_url: str = redis_url_field(required=True)
    api_url: str = api_url_field(required=True)


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance.

    Validates required env vars on first call.
    Raises ValidationError if REDIS_URL or API_URL are missing.
    """
    return Settings()

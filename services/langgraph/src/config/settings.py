"""LangGraph service configuration.

Requires: REDIS_URL, API_BASE_URL
Does NOT need DATABASE_URL (accesses DB via API)
"""

from functools import lru_cache

from shared.config import BaseSettings, api_base_url_field, redis_url_field


class Settings(BaseSettings):
    """LangGraph service settings."""

    # Required
    redis_url: str = redis_url_field(required=True)
    api_base_url: str = api_base_url_field(required=True)


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance.

    Validates required env vars on first call.
    Raises ValidationError if REDIS_URL or API_BASE_URL are missing.
    """
    return Settings()

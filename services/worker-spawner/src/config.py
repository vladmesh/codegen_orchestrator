"""Worker Spawner service configuration.

Requires: REDIS_URL
"""

from functools import lru_cache

from shared.config import BaseSettings, redis_url_field


class Settings(BaseSettings):
    """Worker Spawner service settings."""

    # Required
    redis_url: str = redis_url_field(required=True)


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance.

    Validates required env vars on first call.
    Raises ValidationError if any required var is missing.
    """
    return Settings()

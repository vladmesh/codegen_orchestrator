"""Scaffolder service configuration.

Requires: REDIS_URL, API_BASE_URL, WORKSPACE_BASE_PATH, SERVICE_TEMPLATE_PATH
"""

from functools import lru_cache

from pydantic import Field

from shared.config import BaseSettings, api_base_url_field, redis_url_field


class Settings(BaseSettings):
    """Scaffolder service settings."""

    redis_url: str = redis_url_field(required=True)
    api_base_url: str = api_base_url_field(required=True)
    workspace_base_path: str = Field(
        ...,
        description="Base path for project workspaces (e.g. /data/workspaces)",
    )
    service_template_path: str = Field(
        ...,
        description="Path to service-template repo on disk (for copier)",
    )


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance.

    Validates required env vars on first call.
    Raises ValidationError if any required var is missing.
    """
    return Settings()

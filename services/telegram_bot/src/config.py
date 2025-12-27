"""Telegram Bot service configuration.

Requires: REDIS_URL, API_BASE_URL, TELEGRAM_BOT_TOKEN
"""

from functools import lru_cache

from pydantic import Field

from shared.config import BaseSettings, api_base_url_field, redis_url_field, telegram_token_field


class Settings(BaseSettings):
    """Telegram Bot service settings."""

    # Required
    redis_url: str = redis_url_field(required=True)
    api_base_url: str = api_base_url_field(required=True)
    telegram_bot_token: str = telegram_token_field(required=True)

    # Access Control
    admin_telegram_ids: str = Field(default="", alias="ADMIN_TELEGRAM_IDS")

    def get_admin_ids(self) -> set[int]:
        """Parse comma-separated IDs into set of integers."""
        if not self.admin_telegram_ids:
            return set()
        return {
            int(id_str.strip())
            for id_str in self.admin_telegram_ids.split(",")
            if id_str.strip().isdigit()
        }


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance.

    Validates required env vars on first call.
    Raises ValidationError if any required var is missing.
    """
    return Settings()

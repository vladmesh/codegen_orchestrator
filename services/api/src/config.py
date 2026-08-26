"""API service configuration.

Requires: DATABASE_URL, REDIS_URL
Optional: TELEGRAM_BOT_TOKEN (for notifications)
"""

from functools import lru_cache

from pydantic import Field

from shared.config import (
    BaseSettings,
    database_url_field,
    default_agent_type_field,
    internal_api_key_field,
    redis_url_field,
    telegram_token_field,
)
from shared.contracts.vocab import AgentType, QAExecutorAgentType


class Settings(BaseSettings):
    """API service settings."""

    # Required
    database_url: str = database_url_field(required=True)
    redis_url: str = redis_url_field(required=True)

    # Optional - notifications work without token in dev
    telegram_bot_token: str = telegram_token_field(required=False)

    # LK (user dashboard) JWT auth — required, no default. An empty string would
    # sign every dashboard token with a known key, so reject it outright.
    lk_jwt_secret: str = Field(min_length=1)

    # Internal service token — sent by workers, scheduler, langgraph as X-Internal-Key
    internal_api_key: str = internal_api_key_field()

    # Project creation resolves this at request time, so deployments can change
    # the developer-worker default without changing the PO request contract.
    default_agent_type: AgentType = default_agent_type_field()

    # QA executor policy belongs at paid-run admission, not in the later
    # consumer process. Keep the existing setting name/default while the QA
    # worker still needs its unrelated runtime configuration.
    qa_executor_agent_type: QAExecutorAgentType = AgentType.CODEX

    admin_telegram_ids: str = Field(default="", alias="ADMIN_TELEGRAM_IDS")

    def get_admin_ids(self) -> set[int]:
        """Parse the bot's owner list for the registration exception."""
        return {
            int(value.strip())
            for value in self.admin_telegram_ids.split(",")
            if value.strip().isdigit()
        }


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance.

    Validates required env vars on first call.
    Raises ValidationError if DATABASE_URL or REDIS_URL are missing.
    """
    return Settings()

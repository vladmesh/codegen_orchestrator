"""API service configuration.

Requires: DATABASE_URL, REDIS_URL, WORKER_MANAGER_URL, LK_JWT_SECRET,
INTERNAL_API_KEY, GITHUB_WEBHOOK_SECRET, DEFAULT_AGENT_TYPE
"""

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator

from shared.config import (
    BaseSettings,
    database_url_field,
    default_agent_type_field,
    internal_api_key_field,
    redis_url_field,
)
from shared.contracts.vocab import AgentType, QAExecutorAgentType


class Settings(BaseSettings):
    """API service settings."""

    # Required
    database_url: str = database_url_field(required=True)
    redis_url: str = redis_url_field(required=True)
    worker_manager_url: str = Field(min_length=1)

    # LK (user dashboard) JWT auth — required, no default. An empty string would
    # sign every dashboard token with a known key, so reject it outright.
    lk_jwt_secret: str = Field(min_length=1)

    # Internal service token — sent by workers, scheduler, langgraph as X-Internal-Key
    internal_api_key: str = internal_api_key_field()

    # Signs GitHub webhook deliveries; a dedicated credential no other key derives.
    github_webhook_secret: SecretStr = Field(alias="GITHUB_WEBHOOK_SECRET", repr=False)

    # Project creation resolves this at request time, so deployments can change
    # the developer-worker default without changing the PO request contract.
    default_agent_type: AgentType = default_agent_type_field()

    # QA executor policy belongs at paid-run admission, not in the later
    # consumer process. Keep the existing setting name/default while the QA
    # worker still needs its unrelated runtime configuration.
    qa_executor_agent_type: QAExecutorAgentType = AgentType.CODEX

    admin_telegram_ids: str = Field(default="", alias="ADMIN_TELEGRAM_IDS")

    # Platform services the admin console shows, as `name=https://base-url` pairs separated by
    # commas (e.g. `feeds=https://platform.example/feeds`). Core keeps no service
    # registry; empty shows none beyond the ones deployed products already use.
    platform_services: str = Field(default="", alias="PLATFORM_SERVICES")

    @field_validator("github_webhook_secret")
    @classmethod
    def _refuse_blank_webhook_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("GITHUB_WEBHOOK_SECRET must not be empty or whitespace")
        return value

    def get_platform_services(self) -> dict[str, str]:
        """Parse the declared platform services into name → base URL."""
        services: dict[str, str] = {}
        for pair in self.platform_services.split(","):
            name, _, url = pair.strip().partition("=")
            if name.strip() and url.strip():
                services[name.strip()] = url.strip()
        return services

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
    Raises ValidationError if any required var is missing.
    """
    return Settings()

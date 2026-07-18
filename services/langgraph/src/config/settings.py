"""LangGraph service configuration.

Requires: REDIS_URL, API_BASE_URL
Optional: CHECKPOINT_DATABASE_URL (PostgreSQL for LangGraph checkpointer persistence)
"""

from functools import lru_cache

from shared.config import (
    BaseSettings,
    api_base_url_field,
    default_agent_type_field,
    redis_url_field,
)
from shared.contracts.vocab import AgentType


class Settings(BaseSettings):
    """LangGraph service settings."""

    # Required
    redis_url: str = redis_url_field(required=True)
    api_base_url: str = api_base_url_field(required=True)

    # Worker configuration
    default_agent_type: AgentType = default_agent_type_field()

    # Optional: Mount host Claude session for dev agents (avoids API key need)
    mount_claude_session: bool = True

    # Optional: Override Anthropic API URL for developer workers (E2E testing)
    # When set, workers use this URL instead of api.anthropic.com
    anthropic_base_url: str | None = None

    # Optional: PO ReactAgent LLM config (all three required to enable PO consumer)
    po_llm_model: str | None = None
    po_llm_base_url: str | None = None
    po_llm_api_key: str | None = None

    # Optional: Architect ReactAgent LLM config
    architect_llm_model: str | None = None
    architect_llm_base_url: str | None = None
    architect_llm_api_key: str | None = None

    # Optional: PostgreSQL URL for LangGraph checkpointer persistence
    # Falls back to MemorySaver (in-memory) if not set
    checkpoint_database_url: str | None = None

    # Summarization config (used by SummarizationNode)
    # Defaults here are fallbacks — production reads from system_configs DB via ConfigStore
    summarization_model: str | None = None  # None = fallback to po_llm_model
    summarization_max_tokens: int = 20000
    summarization_trigger_tokens: int = 70000
    summarization_max_summary_tokens: int = 2000


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance.

    Validates required env vars on first call.
    Raises ValidationError if REDIS_URL or API_BASE_URL are missing.
    """
    return Settings()

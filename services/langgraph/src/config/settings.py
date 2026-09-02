"""LangGraph service configuration.

Requires: REDIS_URL, API_BASE_URL
Optional: CHECKPOINT_DATABASE_URL (PostgreSQL for LangGraph checkpointer persistence)
"""

from functools import lru_cache

from pydantic import Field

from shared.allocation_freshness import ALLOCATION_METRICS_FRESHNESS_SECONDS
from shared.config import (
    BaseSettings,
    api_base_url_field,
    default_agent_type_field,
    redis_url_field,
)
from shared.contracts.vocab import AgentType, QAExecutorAgentType


class Settings(BaseSettings):
    """LangGraph service settings."""

    # Required
    redis_url: str = redis_url_field(required=True)
    api_base_url: str = api_base_url_field(required=True)

    # Worker configuration
    default_agent_type: AgentType = default_agent_type_field()

    # Resource allocation admission controls. Both values are intentionally
    # environment-configurable because provider and workload characteristics vary.
    allocation_ram_reserve_mb: int = Field(default=256, ge=0)
    allocation_metrics_freshness_seconds: int = Field(
        default=ALLOCATION_METRICS_FRESHNESS_SECONDS, gt=0
    )

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

    # Who performs exploratory QA. Codex on the management host's isolated
    # subscription session by default; Claude Code remains an explicit override. The
    # type is the narrow one on purpose: `factory` would run QA on a provider
    # API key and `noop` would run no QA at all, so both are refused when the
    # configuration is read rather than at the far end of a started run.
    qa_executor_agent_type: QAExecutorAgentType = AgentType.CODEX
    # How the QA executor's container addresses this runtime's per-run
    # capability endpoint. It is a name on the worker network, not a URL: the
    # port is chosen per run and the token with it.
    qa_capability_host: str = "qa-worker"

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

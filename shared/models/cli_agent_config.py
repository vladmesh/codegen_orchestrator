"""CLI Agent configuration model."""

from typing import Any

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class CLIAgentConfig(Base):
    """Configuration for CLI-based agents (Factory.ai, Claude Code, etc.).

    Stores provider-specific settings, timeouts, and workspace configuration
    distinct from LLM-based agents.
    """

    __tablename__ = "cli_agent_configs"

    # Primary identifier: architect.spawn_factory_worker, developer.spawn_worker
    id: Mapped[str] = mapped_column(String(50), primary_key=True)

    # Display name
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    # Provider: 'factory', 'claude', 'codex', 'generic'
    provider: Mapped[str] = mapped_column(String(50), nullable=False)

    # Common Settings
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=600, nullable=False)
    workspace_image: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # List of required credential keys (e.g. ["GITHUB_TOKEN"])
    required_credentials: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)

    # Provider specific settings (e.g. {"autonomy": "high"})
    provider_settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    def __repr__(self) -> str:
        return f"<CLIAgentConfig(id='{self.id}', provider='{self.provider}')>"

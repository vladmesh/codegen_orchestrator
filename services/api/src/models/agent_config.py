"""Agent configuration model for storing prompts in database."""

from sqlalchemy import Boolean, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AgentConfig(Base):
    """Configuration for LangGraph agents.

    Stores system prompts, model settings, and other configuration
    that can be managed via admin panel.
    """

    __tablename__ = "agent_configs"

    # Primary identifier: product_owner, architect, zavhoz, brainstorm, developer
    id: Mapped[str] = mapped_column(String(50), primary_key=True)

    # Display name for admin panel
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    # Full system prompt text
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)

    # LLM configuration
    model_name: Mapped[str] = mapped_column(String(100), default="gpt-4o", nullable=False)
    temperature: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # Enable/disable agent
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Versioning for prompt updates
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    def __repr__(self) -> str:
        return f"<AgentConfig(id='{self.id}', name='{self.name}', model='{self.model_name}')>"

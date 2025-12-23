"""API Key model."""

from sqlalchemy import String, Integer
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class APIKey(Base):
    """API Key model (e.g. for OpenAI, Anthropic)."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service: Mapped[str] = mapped_column(String(50)) # openai, anthropic
    key_enc: Mapped[str] = mapped_column(String) # Encrypted key
    type: Mapped[str] = mapped_column(String(20), default="system") # system, project
    
    # Optional project linkage for project-specific keys
    project_id: Mapped[str] = mapped_column(String(255), nullable=True)

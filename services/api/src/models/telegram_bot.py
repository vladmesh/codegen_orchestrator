"""Telegram Bot entity."""

from sqlalchemy import String, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class TelegramBot(Base):
    """Telegram Bot model."""

    __tablename__ = "telegram_bots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_enc: Mapped[str] = mapped_column(String)  # Encrypted token
    username: Mapped[str] = mapped_column(String(255))
    tg_id: Mapped[str] = mapped_column(String(255), nullable=True) # ID from Telegram API
    
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=True)

"""System configuration model for externalizing operational constants."""

from sqlalchemy import JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class SystemConfig(Base):
    """Key-value configuration stored in database.

    Replaces hardcoded constants (polling intervals, thresholds, etc.)
    with DB-backed values editable via admin panel.
    """

    __tablename__ = "system_configs"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool] = mapped_column(JSON, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)

    def __repr__(self) -> str:
        return f"<SystemConfig(key='{self.key}', category='{self.category}')>"

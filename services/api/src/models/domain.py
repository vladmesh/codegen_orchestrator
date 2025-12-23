"""Domain model."""

from sqlalchemy import String, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Domain(Base):
    """Domain model."""

    __tablename__ = "domains"

    domain_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    provider: Mapped[str] = mapped_column(String(50), default="cloudflare")
    
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=True)

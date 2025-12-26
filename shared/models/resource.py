"""Resource model."""

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Resource(Base):
    """Resource model - stores handles and metadata for secrets."""

    __tablename__ = "resources"

    handle: Mapped[str] = mapped_column(String(255), primary_key=True)
    resource_type: Mapped[str] = mapped_column(String(50), index=True)
    name: Mapped[str] = mapped_column(String(255))
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict)

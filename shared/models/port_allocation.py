"""Port allocation model."""

import uuid

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class PortAllocation(Base):
    """Port allocation model - tracks used ports on servers."""

    __tablename__ = "port_allocations"
    __table_args__ = (UniqueConstraint("server_handle", "port", name="uq_server_port"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    server_handle: Mapped[str] = mapped_column(ForeignKey("servers.handle"), index=True)
    port: Mapped[int] = mapped_column(Integer)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id"), nullable=True
    )
    service_name: Mapped[str] = mapped_column(String(255))

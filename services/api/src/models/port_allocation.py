"""Port allocation model."""

from sqlalchemy import String, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class PortAllocation(Base):
    """Port allocation model - tracks used ports on servers."""

    __tablename__ = "port_allocations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    server_handle: Mapped[str] = mapped_column(ForeignKey("servers.handle"), index=True)
    port: Mapped[int] = mapped_column(Integer)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=True)
    service_name: Mapped[str] = mapped_column(String(255))

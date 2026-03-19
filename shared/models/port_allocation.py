"""Port allocation model."""

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class PortAllocation(Base):
    """Port allocation model - tracks used ports on servers.

    Belongs to an Application — the runtime unit on a server.
    """

    __tablename__ = "port_allocations"
    __table_args__ = (UniqueConstraint("server_handle", "port", name="uq_server_port"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    server_handle: Mapped[str] = mapped_column(ForeignKey("servers.handle"), index=True)
    port: Mapped[int] = mapped_column(Integer)
    application_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("applications.id"), nullable=True, index=True
    )
    service_name: Mapped[str] = mapped_column(String(255))

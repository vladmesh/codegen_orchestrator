"""Server model."""

from typing import Optional

from sqlalchemy import JSON, String, Integer
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Server(Base):
    """Server model - represents a VPS/Dedicated server."""

    __tablename__ = "servers"

    handle: Mapped[str] = mapped_column(String(255), primary_key=True)
    host: Mapped[str] = mapped_column(String(255))
    public_ip: Mapped[str] = mapped_column(String(255))
    ssh_user: Mapped[str] = mapped_column(String(50), default="root")
    # Store encrypted keys
    ssh_key_enc: Mapped[Optional[str]] = mapped_column(String) 
    
    # Capacity metrics
    capacity_ram_mb: Mapped[int] = mapped_column(Integer, default=1024)
    
    # Management flags
    is_managed: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str] = mapped_column(String(50), default="active") # active, maintenance, reserved, discovered, missing
    notes: Mapped[Optional[str]] = mapped_column(String)
    
    labels: Mapped[dict] = mapped_column(JSON, default=dict)

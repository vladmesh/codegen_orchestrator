"""Server metrics history model — time-series snapshots with 7-day retention."""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base


class ServerMetricsHistory(Base):
    """Time-series metrics snapshots for servers."""

    __tablename__ = "server_metrics_history"
    __table_args__ = (
        Index("ix_server_metrics_history_handle_recorded", "server_handle", "recorded_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    server_handle: Mapped[str] = mapped_column(
        ForeignKey("servers.handle"), index=True, nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False)

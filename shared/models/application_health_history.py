"""Application health history model — time-series snapshots with 7-day retention."""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base


class ApplicationHealthHistory(Base):
    """Time-series health check snapshots for applications."""

    __tablename__ = "application_health_history"
    __table_args__ = (
        Index(
            "ix_app_health_history_app_recorded",
            "application_id",
            "recorded_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), index=True, nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False)

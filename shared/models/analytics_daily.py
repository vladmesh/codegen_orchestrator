"""AnalyticsDaily model — per-project per-day rollup from hourly data."""

import datetime as dt
import uuid

from sqlalchemy import Date, Float, ForeignKey, Integer, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AnalyticsDaily(Base):
    """One row = one day of one project (all services summed)."""

    __tablename__ = "analytics_daily"
    __table_args__ = (
        UniqueConstraint("project_id", "date", name="uq_analytics_daily_project_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id"), nullable=False, index=True
    )
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)

    total_requests: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    unique_users: Mapped[int] = mapped_column(Integer, default=0)
    new_users: Mapped[int] = mapped_column(Integer, default=0)
    dau: Mapped[int] = mapped_column(Integer, default=0)
    returning_users: Mapped[int] = mapped_column(Integer, default=0)

    p95_ms: Mapped[float] = mapped_column(Float, nullable=True)
    error_rate: Mapped[float] = mapped_column(Float, nullable=True)

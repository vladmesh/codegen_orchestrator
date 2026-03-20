"""AnalyticsHourly model — per-service per-project per-hour metrics."""

from datetime import datetime

from sqlalchemy import (
    JSON as SA_JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AnalyticsHourly(Base):
    """One row = one hour of one service of one project."""

    __tablename__ = "analytics_hourly"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "service_name",
            "bucket",
            name="uq_analytics_hourly_project_service_bucket",
        ),
        Index("ix_analytics_hourly_project_bucket", "project_id", "bucket"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        Uuid, ForeignKey("projects.id"), nullable=False, index=True
    )
    service_name: Mapped[str] = mapped_column(String(100), nullable=False)
    bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    total_requests: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    unique_users: Mapped[int] = mapped_column(Integer, default=0)
    new_users: Mapped[int] = mapped_column(Integer, default=0)

    p50_ms: Mapped[float] = mapped_column(Float, nullable=True)
    p95_ms: Mapped[float] = mapped_column(Float, nullable=True)
    p99_ms: Mapped[float] = mapped_column(Float, nullable=True)

    top_endpoints: Mapped[dict | None] = mapped_column(SA_JSON, nullable=True)

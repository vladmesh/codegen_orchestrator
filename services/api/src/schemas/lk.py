"""LK (user dashboard) API schemas."""

import datetime as dt
from enum import StrEnum
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict

# --- Auth ---


class TokenExchangeRequest(BaseModel):
    """Exchange a one-time token for a JWT."""

    token: str


class TokenExchangeResponse(BaseModel):
    """JWT response after successful token exchange."""

    access_token: str
    token_type: str = "bearer"  # noqa: S105 — not a password, OAuth2 convention


# --- Projects list ---


class LatestDailySummary(BaseModel):
    """Latest daily analytics summary for project list view."""

    date: dt.date
    total_requests: int
    unique_users: int
    error_rate: float | None
    p95_ms: float | None

    model_config = ConfigDict(from_attributes=True)


class LkProject(BaseModel):
    """Project in the LK project list."""

    id: uuid.UUID
    name: str
    status: str
    latest_daily: LatestDailySummary | None


# --- Summary ---


class SummaryPeriod(StrEnum):
    """Allowed summary periods."""

    H24 = "24h"
    D7 = "7d"
    D30 = "30d"


class ServiceBreakdown(BaseModel):
    """Per-service metrics breakdown."""

    service_name: str
    total_requests: int
    error_count: int
    unique_users: int
    p95_ms: float | None


class ProjectSummaryResponse(BaseModel):
    """Aggregated project summary for a given period."""

    total_users: int
    new_users: int
    dau: int
    wau: int
    returning_pct: float
    total_requests: int
    error_rate: float
    p95_ms: float | None
    top_endpoints: list[dict[str, Any]]
    breakdown: list[ServiceBreakdown]


# --- Chart ---


class ChartMetric(StrEnum):
    """Allowed chart metrics."""

    USERS = "users"
    REQUESTS = "requests"
    ERRORS = "errors"


class ChartDataPoint(BaseModel):
    """Single data point for a chart."""

    date: str
    value: float


class ChartResponse(BaseModel):
    """Time series chart data."""

    metric: ChartMetric
    period: SummaryPeriod
    data: list[ChartDataPoint]


# --- Status ---


class ServiceStatus(BaseModel):
    """Health status of a single service."""

    name: str
    status: str  # "up" or "down"
    last_seen: dt.datetime | None


class ProjectStatusResponse(BaseModel):
    """Service health status for a project."""

    services: list[ServiceStatus]

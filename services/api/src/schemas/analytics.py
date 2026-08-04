"""Analytics API schemas."""

import datetime as dt
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict

# The upsert schemas are the contract every client already imports; the API
# validates against that same object rather than a look-alike of its own.
from shared.contracts.dto.analytics import (
    AnalyticsDailyCreate,
    AnalyticsHourlyCreate,
    AnalyticsKnownUsersBatchUpsert,
    AnalyticsKnownUserUpsert,
)
from shared.contracts.dto.base import TimestampedDTO

__all__ = [
    "AnalyticsDailyCreate",
    "AnalyticsDailyRead",
    "AnalyticsHourlyCreate",
    "AnalyticsHourlyRead",
    "AnalyticsKnownUserRead",
    "AnalyticsKnownUserUpsert",
    "AnalyticsKnownUsersBatchUpsert",
]

# --- Hourly ---


class AnalyticsHourlyRead(TimestampedDTO):
    """Hourly analytics response."""

    id: int
    project_id: uuid.UUID
    service_name: str
    bucket: dt.datetime

    total_requests: int
    error_count: int
    unique_users: int
    new_users: int

    p50_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None

    top_endpoints: list[dict[str, Any]] | None = None

    model_config = ConfigDict(from_attributes=True)


# --- Daily ---


class AnalyticsDailyRead(TimestampedDTO):
    """Daily analytics response."""

    id: int
    project_id: uuid.UUID
    date: dt.date

    total_requests: int
    error_count: int
    unique_users: int
    new_users: int
    dau: int
    returning_users: int

    p95_ms: float | None = None
    error_rate: float | None = None

    model_config = ConfigDict(from_attributes=True)


# --- Known Users ---


class AnalyticsKnownUserRead(BaseModel):
    """Known user response."""

    project_id: uuid.UUID
    user_id_hash: str
    first_seen: dt.datetime
    last_seen: dt.datetime

    model_config = ConfigDict(from_attributes=True)

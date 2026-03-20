"""Analytics API schemas."""

import datetime as dt
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict

from shared.contracts.dto.base import TimestampedDTO

# --- Hourly ---


class AnalyticsHourlyCreate(BaseModel):
    """Upsert hourly analytics."""

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


class AnalyticsDailyCreate(BaseModel):
    """Upsert daily analytics."""

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


class AnalyticsKnownUserUpsert(BaseModel):
    """Single known user entry for batch upsert."""

    user_id_hash: str
    first_seen: dt.datetime
    last_seen: dt.datetime


class AnalyticsKnownUsersBatchUpsert(BaseModel):
    """Batch upsert known users for a project."""

    project_id: uuid.UUID
    users: list[AnalyticsKnownUserUpsert]


class AnalyticsKnownUserRead(BaseModel):
    """Known user response."""

    project_id: uuid.UUID
    user_id_hash: str
    first_seen: dt.datetime
    last_seen: dt.datetime

    model_config = ConfigDict(from_attributes=True)

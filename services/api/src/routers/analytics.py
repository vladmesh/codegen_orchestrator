"""Analytics router — CRUD for hourly, daily, and known_users tables."""

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.analytics_daily import AnalyticsDaily
from shared.models.analytics_hourly import AnalyticsHourly
from shared.models.analytics_known_users import AnalyticsKnownUsers

from ..database import get_async_session
from ..schemas.analytics import (
    AnalyticsDailyCreate,
    AnalyticsDailyRead,
    AnalyticsHourlyCreate,
    AnalyticsHourlyRead,
    AnalyticsKnownUserRead,
    AnalyticsKnownUsersBatchUpsert,
)

router = APIRouter(prefix="/analytics", tags=["analytics"])


# --- Hourly ---


@router.post(
    "/hourly",
    response_model=AnalyticsHourlyRead,
    status_code=status.HTTP_201_CREATED,
)
async def upsert_hourly(
    data: AnalyticsHourlyCreate,
    db: AsyncSession = Depends(get_async_session),
) -> AnalyticsHourly:
    """Upsert an hourly analytics row (insert or update on conflict)."""
    values = data.model_dump()
    stmt = pg_insert(AnalyticsHourly).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["project_id", "service_name", "bucket"],
        set_={
            "total_requests": stmt.excluded.total_requests,
            "error_count": stmt.excluded.error_count,
            "unique_users": stmt.excluded.unique_users,
            "new_users": stmt.excluded.new_users,
            "p50_ms": stmt.excluded.p50_ms,
            "p95_ms": stmt.excluded.p95_ms,
            "p99_ms": stmt.excluded.p99_ms,
            "top_endpoints": stmt.excluded.top_endpoints,
        },
    )
    await db.execute(stmt)
    await db.commit()

    # Fetch the upserted row
    row = (
        await db.execute(
            select(AnalyticsHourly).where(
                AnalyticsHourly.project_id == data.project_id,
                AnalyticsHourly.service_name == data.service_name,
                AnalyticsHourly.bucket == data.bucket,
            )
        )
    ).scalar_one()
    return row


@router.get("/hourly", response_model=list[AnalyticsHourlyRead])
async def list_hourly(
    project_id: uuid.UUID = Query(...),
    start: dt.datetime | None = Query(None),
    end: dt.datetime | None = Query(None),
    service_name: str | None = Query(None),
    db: AsyncSession = Depends(get_async_session),
) -> list[AnalyticsHourly]:
    """List hourly analytics for a project, optionally filtered by time range."""
    query = select(AnalyticsHourly).where(AnalyticsHourly.project_id == project_id)
    if start:
        query = query.where(AnalyticsHourly.bucket >= start)
    if end:
        query = query.where(AnalyticsHourly.bucket < end)
    if service_name:
        query = query.where(AnalyticsHourly.service_name == service_name)
    query = query.order_by(AnalyticsHourly.bucket.asc())

    result = await db.execute(query)
    return result.scalars().all()


@router.delete("/hourly")
async def delete_old_hourly(
    older_than_days: int = Query(..., description="Delete rows older than N days"),
    db: AsyncSession = Depends(get_async_session),
) -> dict[str, int]:
    """Delete hourly analytics older than the specified number of days."""
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=older_than_days)
    result = await db.execute(delete(AnalyticsHourly).where(AnalyticsHourly.bucket < cutoff))
    await db.commit()
    return {"deleted": result.rowcount}


# --- Daily ---


@router.post(
    "/daily",
    response_model=AnalyticsDailyRead,
    status_code=status.HTTP_201_CREATED,
)
async def upsert_daily(
    data: AnalyticsDailyCreate,
    db: AsyncSession = Depends(get_async_session),
) -> AnalyticsDaily:
    """Upsert a daily analytics row."""
    values = data.model_dump()
    stmt = pg_insert(AnalyticsDaily).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["project_id", "date"],
        set_={
            "total_requests": stmt.excluded.total_requests,
            "error_count": stmt.excluded.error_count,
            "unique_users": stmt.excluded.unique_users,
            "new_users": stmt.excluded.new_users,
            "dau": stmt.excluded.dau,
            "returning_users": stmt.excluded.returning_users,
            "p95_ms": stmt.excluded.p95_ms,
            "error_rate": stmt.excluded.error_rate,
        },
    )
    await db.execute(stmt)
    await db.commit()

    row = (
        await db.execute(
            select(AnalyticsDaily).where(
                AnalyticsDaily.project_id == data.project_id,
                AnalyticsDaily.date == data.date,
            )
        )
    ).scalar_one()
    return row


@router.get("/daily", response_model=list[AnalyticsDailyRead])
async def list_daily(
    project_id: uuid.UUID = Query(...),
    start: dt.date | None = Query(None),
    end: dt.date | None = Query(None),
    db: AsyncSession = Depends(get_async_session),
) -> list[AnalyticsDaily]:
    """List daily analytics for a project."""
    query = select(AnalyticsDaily).where(AnalyticsDaily.project_id == project_id)
    if start:
        query = query.where(AnalyticsDaily.date >= start)
    if end:
        query = query.where(AnalyticsDaily.date < end)
    query = query.order_by(AnalyticsDaily.date.asc())

    result = await db.execute(query)
    return result.scalars().all()


@router.delete("/daily")
async def delete_old_daily(
    older_than_days: int = Query(..., description="Delete rows older than N days"),
    db: AsyncSession = Depends(get_async_session),
) -> dict[str, int]:
    """Delete daily analytics older than the specified number of days."""
    cutoff = dt.date.today() - dt.timedelta(days=older_than_days)
    result = await db.execute(delete(AnalyticsDaily).where(AnalyticsDaily.date < cutoff))
    await db.commit()
    return {"deleted": result.rowcount}


# --- Known Users ---


@router.post("/known-users", status_code=status.HTTP_201_CREATED)
async def batch_upsert_known_users(
    data: AnalyticsKnownUsersBatchUpsert,
    db: AsyncSession = Depends(get_async_session),
) -> dict[str, int]:
    """Batch upsert known users for a project."""
    if not data.users:
        return {"upserted": 0}

    for user in data.users:
        stmt = pg_insert(AnalyticsKnownUsers).values(
            project_id=data.project_id,
            user_id_hash=user.user_id_hash,
            first_seen=user.first_seen,
            last_seen=user.last_seen,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["project_id", "user_id_hash"],
            set_={
                "last_seen": stmt.excluded.last_seen,
            },
        )
        await db.execute(stmt)

    await db.commit()
    return {"upserted": len(data.users)}


@router.get("/known-users", response_model=list[AnalyticsKnownUserRead])
async def list_known_users(
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_async_session),
) -> list[AnalyticsKnownUsers]:
    """List known users for a project."""
    result = await db.execute(
        select(AnalyticsKnownUsers).where(AnalyticsKnownUsers.project_id == project_id)
    )
    return result.scalars().all()

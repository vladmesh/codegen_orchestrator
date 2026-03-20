"""LK (user dashboard) router — analytics endpoints for project owners."""

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import User
from shared.models.analytics_daily import AnalyticsDaily
from shared.models.analytics_hourly import AnalyticsHourly
from shared.models.analytics_known_users import AnalyticsKnownUsers
from shared.models.project import Project

from ..database import get_async_session
from ..dependencies import get_lk_user
from ..schemas.lk import (
    ChartDataPoint,
    ChartMetric,
    ChartResponse,
    LatestDailySummary,
    LkProject,
    ProjectStatusResponse,
    ProjectSummaryResponse,
    ServiceBreakdown,
    ServiceStatus,
    SummaryPeriod,
)

router = APIRouter(prefix="/lk", tags=["lk"])

# How old the latest hourly bucket can be before we consider the service "down"
_STATUS_UP_THRESHOLD = dt.timedelta(hours=2)


async def _get_owned_project(
    project_id: uuid.UUID,
    user: User,
    db: AsyncSession,
) -> Project:
    """Fetch a project and verify ownership. Raises 404 or 403."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    if project.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    return project


# ---------------------------------------------------------------------------
# GET /api/lk/projects — list user's projects with latest daily summary
# ---------------------------------------------------------------------------


@router.get("/projects", response_model=list[LkProject])
async def list_projects(
    user: User = Depends(get_lk_user),
    db: AsyncSession = Depends(get_async_session),
) -> list[LkProject]:
    """List projects owned by the current user with latest daily summary."""
    result = await db.execute(
        select(Project).where(Project.owner_id == user.id).order_by(Project.name)
    )
    projects = result.scalars().all()

    response = []
    for project in projects:
        # Get latest daily summary
        daily_result = await db.execute(
            select(AnalyticsDaily)
            .where(AnalyticsDaily.project_id == project.id)
            .order_by(desc(AnalyticsDaily.date))
            .limit(1)
        )
        daily = daily_result.scalar_one_or_none()

        latest_daily = None
        if daily:
            latest_daily = LatestDailySummary(
                date=daily.date,
                total_requests=daily.total_requests,
                unique_users=daily.unique_users,
                error_rate=daily.error_rate,
                p95_ms=daily.p95_ms,
            )

        response.append(
            LkProject(
                id=project.id,
                name=project.name,
                status=project.status,
                latest_daily=latest_daily,
            )
        )

    return response


# ---------------------------------------------------------------------------
# GET /api/lk/projects/{id}/summary — aggregated metrics
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/summary", response_model=ProjectSummaryResponse)
async def project_summary(
    project_id: uuid.UUID,
    period: SummaryPeriod = Query(SummaryPeriod.D7),
    user: User = Depends(get_lk_user),
    db: AsyncSession = Depends(get_async_session),
) -> ProjectSummaryResponse:
    """Aggregated project metrics for a given period."""
    await _get_owned_project(project_id, user, db)

    now = dt.datetime.now(dt.UTC)

    if period == SummaryPeriod.H24:
        # Use hourly data for 24h period
        cutoff = now - dt.timedelta(hours=24)
        return await _summary_from_hourly(project_id, cutoff, now, db)

    # 7d / 30d — use daily data
    days = 7 if period == SummaryPeriod.D7 else 30
    cutoff_date = dt.date.today() - dt.timedelta(days=days)
    return await _summary_from_daily(project_id, cutoff_date, days, db)


async def _summary_from_hourly(
    project_id: uuid.UUID,
    cutoff: dt.datetime,
    now: dt.datetime,
    db: AsyncSession,
) -> ProjectSummaryResponse:
    """Build summary from analytics_hourly rows."""
    result = await db.execute(
        select(AnalyticsHourly)
        .where(
            AnalyticsHourly.project_id == project_id,
            AnalyticsHourly.bucket >= cutoff,
        )
        .order_by(AnalyticsHourly.bucket)
    )
    rows = result.scalars().all()

    if not rows:
        return _empty_summary()

    # Aggregate across all rows
    total_requests = sum(r.total_requests for r in rows)
    error_count = sum(r.error_count for r in rows)
    total_users = sum(r.unique_users for r in rows)
    new_users = sum(r.new_users for r in rows)
    p95_values = [r.p95_ms for r in rows if r.p95_ms is not None]
    p95_ms = max(p95_values) if p95_values else None
    error_rate = error_count / total_requests if total_requests > 0 else 0.0

    # Top endpoints: merge across all rows
    top_endpoints = _merge_top_endpoints(rows)

    # Breakdown per service
    breakdown = _breakdown_from_hourly(rows)

    # WAU: count distinct known users active in last 7 days
    wau_cutoff = now - dt.timedelta(days=7)
    wau_result = await db.execute(
        select(func.count(AnalyticsKnownUsers.user_id_hash)).where(
            AnalyticsKnownUsers.project_id == project_id,
            AnalyticsKnownUsers.last_seen >= wau_cutoff,
        )
    )
    wau = wau_result.scalar() or 0

    returning_pct = 0.0
    if total_users > 0:
        returning_pct = round(max(0, total_users - new_users) / total_users * 100, 1)

    return ProjectSummaryResponse(
        total_users=total_users,
        new_users=new_users,
        dau=total_users,  # For 24h, DAU ≈ total unique users
        wau=wau,
        returning_pct=returning_pct,
        total_requests=total_requests,
        error_rate=round(error_rate, 4),
        p95_ms=p95_ms,
        top_endpoints=top_endpoints,
        breakdown=breakdown,
    )


async def _summary_from_daily(
    project_id: uuid.UUID,
    cutoff_date: dt.date,
    days: int,
    db: AsyncSession,
) -> ProjectSummaryResponse:
    """Build summary from analytics_daily rows."""
    result = await db.execute(
        select(AnalyticsDaily)
        .where(
            AnalyticsDaily.project_id == project_id,
            AnalyticsDaily.date >= cutoff_date,
        )
        .order_by(AnalyticsDaily.date)
    )
    rows = result.scalars().all()

    if not rows:
        return _empty_summary()

    total_requests = sum(r.total_requests for r in rows)
    error_count = sum(r.error_count for r in rows)
    total_users = sum(r.unique_users for r in rows)
    new_users = sum(r.new_users for r in rows)
    p95_values = [r.p95_ms for r in rows if r.p95_ms is not None]
    p95_ms = max(p95_values) if p95_values else None
    error_rate = error_count / total_requests if total_requests > 0 else 0.0

    # Latest DAU
    dau = rows[-1].dau if rows else 0

    # WAU: sum of unique_users over last 7 days (approximation from daily)
    wau_rows = [r for r in rows if r.date >= dt.date.today() - dt.timedelta(days=7)]
    wau = sum(r.unique_users for r in wau_rows)

    returning_pct = 0.0
    if total_users > 0:
        returning_pct = round(max(0, total_users - new_users) / total_users * 100, 1)

    # Per-service breakdown: need hourly data
    hourly_cutoff = dt.datetime.combine(cutoff_date, dt.time.min, tzinfo=dt.UTC)
    hourly_result = await db.execute(
        select(AnalyticsHourly).where(
            AnalyticsHourly.project_id == project_id,
            AnalyticsHourly.bucket >= hourly_cutoff,
        )
    )
    hourly_rows = hourly_result.scalars().all()
    breakdown = _breakdown_from_hourly(hourly_rows)

    # Top endpoints from hourly
    top_endpoints = _merge_top_endpoints(hourly_rows) if hourly_rows else []

    return ProjectSummaryResponse(
        total_users=total_users,
        new_users=new_users,
        dau=dau,
        wau=wau,
        returning_pct=returning_pct,
        total_requests=total_requests,
        error_rate=round(error_rate, 4),
        p95_ms=p95_ms,
        top_endpoints=top_endpoints,
        breakdown=breakdown,
    )


def _empty_summary() -> ProjectSummaryResponse:
    return ProjectSummaryResponse(
        total_users=0,
        new_users=0,
        dau=0,
        wau=0,
        returning_pct=0.0,
        total_requests=0,
        error_rate=0.0,
        p95_ms=None,
        top_endpoints=[],
        breakdown=[],
    )


def _merge_top_endpoints(rows) -> list[dict]:
    """Merge top_endpoints from multiple hourly rows into a combined top-5."""
    merged: dict[str, int] = {}
    for r in rows:
        if not r.top_endpoints:
            continue
        for ep in r.top_endpoints:
            path = ep["path"]
            merged[path] = merged.get(path, 0) + ep["count"]

    sorted_eps = sorted(merged.items(), key=lambda x: x[1], reverse=True)[:5]
    return [{"path": path, "count": count} for path, count in sorted_eps]


def _breakdown_from_hourly(rows) -> list[ServiceBreakdown]:
    """Group hourly rows by service_name and aggregate."""
    by_service: dict[str, list] = {}
    for r in rows:
        by_service.setdefault(r.service_name, []).append(r)

    breakdown = []
    for svc, svc_rows in sorted(by_service.items()):
        total_req = sum(r.total_requests for r in svc_rows)
        err_count = sum(r.error_count for r in svc_rows)
        unique = sum(r.unique_users for r in svc_rows)
        p95_vals = [r.p95_ms for r in svc_rows if r.p95_ms is not None]
        breakdown.append(
            ServiceBreakdown(
                service_name=svc,
                total_requests=total_req,
                error_count=err_count,
                unique_users=unique,
                p95_ms=max(p95_vals) if p95_vals else None,
            )
        )
    return breakdown


# ---------------------------------------------------------------------------
# GET /api/lk/projects/{id}/chart — time series
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/chart", response_model=ChartResponse)
async def project_chart(
    project_id: uuid.UUID,
    metric: ChartMetric = Query(...),
    period: SummaryPeriod = Query(SummaryPeriod.D7),
    user: User = Depends(get_lk_user),
    db: AsyncSession = Depends(get_async_session),
) -> ChartResponse:
    """Time series data for charts."""
    await _get_owned_project(project_id, user, db)

    days = {"24h": 1, "7d": 7, "30d": 30}[period.value]
    cutoff_date = dt.date.today() - dt.timedelta(days=days)

    result = await db.execute(
        select(AnalyticsDaily)
        .where(
            AnalyticsDaily.project_id == project_id,
            AnalyticsDaily.date >= cutoff_date,
        )
        .order_by(AnalyticsDaily.date)
    )
    rows = result.scalars().all()

    # Map metric to field
    field_map = {
        ChartMetric.USERS: "unique_users",
        ChartMetric.REQUESTS: "total_requests",
        ChartMetric.ERRORS: "error_rate",
    }
    field = field_map[metric]

    data = [
        ChartDataPoint(
            date=str(r.date),
            value=float(getattr(r, field) or 0),
        )
        for r in rows
    ]

    return ChartResponse(metric=metric, period=period, data=data)


# ---------------------------------------------------------------------------
# GET /api/lk/projects/{id}/status — service health
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/status", response_model=ProjectStatusResponse)
async def project_status(
    project_id: uuid.UUID,
    user: User = Depends(get_lk_user),
    db: AsyncSession = Depends(get_async_session),
) -> ProjectStatusResponse:
    """Service health status based on latest analytics data."""
    await _get_owned_project(project_id, user, db)

    now = dt.datetime.now(dt.UTC)

    # Get latest hourly bucket per service_name
    subq = (
        select(
            AnalyticsHourly.service_name,
            func.max(AnalyticsHourly.bucket).label("last_bucket"),
        )
        .where(AnalyticsHourly.project_id == project_id)
        .group_by(AnalyticsHourly.service_name)
        .subquery()
    )

    result = await db.execute(select(subq))
    rows = result.all()

    services = []
    for row in rows:
        last_bucket = row.last_bucket
        is_up = (now - last_bucket) < _STATUS_UP_THRESHOLD
        services.append(
            ServiceStatus(
                name=row.service_name,
                status="up" if is_up else "down",
                last_seen=last_bucket,
            )
        )

    return ProjectStatusResponse(services=services)

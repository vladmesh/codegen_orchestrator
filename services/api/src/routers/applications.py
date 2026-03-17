"""Applications router — runtime state of deployed units."""

from datetime import UTC

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Application

from ..database import get_async_session
from ..schemas import (
    ApplicationCreate,
    ApplicationHealthHistoryCreate,
    ApplicationHealthHistoryRead,
    ApplicationRead,
    ApplicationUpdate,
)

router = APIRouter(prefix="/applications", tags=["applications"])


@router.post("/", response_model=ApplicationRead, status_code=status.HTTP_201_CREATED)
async def create_application(
    app_in: ApplicationCreate,
    db: AsyncSession = Depends(get_async_session),
) -> Application:
    """Create a new application record."""
    application = Application(
        repo_id=app_in.repo_id,
        server_handle=app_in.server_handle,
        service_name=app_in.service_name,
        status=app_in.status,
    )
    db.add(application)
    await db.commit()
    await db.refresh(application)
    return application


@router.get("/", response_model=list[ApplicationRead])
async def list_applications(
    server_handle: str | None = Query(None),
    status: str | None = Query(None),
    repo_id: str | None = Query(None),
    db: AsyncSession = Depends(get_async_session),
) -> list[Application]:
    """List applications with optional filtering."""
    query = select(Application)

    if server_handle is not None:
        query = query.where(Application.server_handle == server_handle)
    if status is not None:
        query = query.where(Application.status == status)
    if repo_id is not None:
        query = query.where(Application.repo_id == repo_id)

    query = query.order_by(Application.service_name)
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{application_id}", response_model=ApplicationRead)
async def get_application(
    application_id: int,
    db: AsyncSession = Depends(get_async_session),
) -> Application:
    """Get application by ID."""
    application = await db.get(Application, application_id)
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    return application


@router.patch("/{application_id}", response_model=ApplicationRead)
async def update_application(
    application_id: int,
    app_update: ApplicationUpdate,
    db: AsyncSession = Depends(get_async_session),
) -> Application:
    """Update application status and metadata."""
    application = await db.get(Application, application_id)
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")

    if app_update.status is not None:
        application.status = app_update.status
    if app_update.last_health_check is not None:
        application.last_health_check = app_update.last_health_check
    if app_update.response_time_ms is not None:
        application.response_time_ms = app_update.response_time_ms
    if app_update.ssl_expires_at is not None:
        application.ssl_expires_at = app_update.ssl_expires_at
    if app_update.uptime_pct_24h is not None:
        application.uptime_pct_24h = app_update.uptime_pct_24h

    await db.commit()
    await db.refresh(application)
    return application


# ---------------------------------------------------------------------------
# Health history endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/{application_id}/health-history",
    response_model=list[ApplicationHealthHistoryRead],
)
async def get_health_history(
    application_id: int,
    hours: int = 24,
    db: AsyncSession = Depends(get_async_session),
) -> list:
    """Get health check history for an application."""
    from datetime import datetime, timedelta

    from shared.models import ApplicationHealthHistory

    if not await db.get(Application, application_id):
        raise HTTPException(status_code=404, detail="Application not found")

    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    query = (
        select(ApplicationHealthHistory)
        .where(
            ApplicationHealthHistory.application_id == application_id,
            ApplicationHealthHistory.recorded_at >= cutoff,
        )
        .order_by(ApplicationHealthHistory.recorded_at.desc())
    )

    result = await db.execute(query)
    return result.scalars().all()


@router.delete("/health-history")
async def delete_old_health_history(
    retention_hours: int = 168,
    db: AsyncSession = Depends(get_async_session),
) -> dict:
    """Delete health history older than retention_hours (default 7 days)."""
    from datetime import datetime, timedelta

    from sqlalchemy import delete as sa_delete

    from shared.models import ApplicationHealthHistory

    cutoff = datetime.now(UTC) - timedelta(hours=retention_hours)
    stmt = sa_delete(ApplicationHealthHistory).where(ApplicationHealthHistory.recorded_at < cutoff)
    result = await db.execute(stmt)
    await db.commit()
    return {"deleted": result.rowcount}


@router.post(
    "/{application_id}/health-history",
    response_model=ApplicationHealthHistoryRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_health_history(
    application_id: int,
    snapshot: ApplicationHealthHistoryCreate,
    db: AsyncSession = Depends(get_async_session),
) -> object:
    """Append a health history snapshot for an application (internal use)."""
    from shared.models import ApplicationHealthHistory

    if not await db.get(Application, application_id):
        raise HTTPException(status_code=404, detail="Application not found")

    entry = ApplicationHealthHistory(
        application_id=application_id,
        metrics=snapshot.metrics,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry

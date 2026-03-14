"""Applications router — runtime state of deployed units."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Application

from ..database import get_async_session
from ..schemas import ApplicationCreate, ApplicationRead, ApplicationUpdate

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
        port=app_in.port,
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
    if app_update.port is not None:
        application.port = app_update.port
    if app_update.last_health_check is not None:
        application.last_health_check = app_update.last_health_check

    await db.commit()
    await db.refresh(application)
    return application

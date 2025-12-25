"""Incidents router."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_async_session
from ..models import Incident, IncidentStatus
from ..schemas import IncidentCreate, IncidentRead, IncidentUpdate

router = APIRouter(prefix="/incidents", tags=["incidents"])


@router.post("/", response_model=IncidentRead, status_code=status.HTTP_201_CREATED)
async def create_incident(
    incident_in: IncidentCreate,
    db: AsyncSession = Depends(get_async_session),
) -> Incident:
    """Create a new incident."""
    incident = Incident(
        server_handle=incident_in.server_handle,
        incident_type=incident_in.incident_type,
        details=incident_in.details,
        affected_services=incident_in.affected_services,
    )
    db.add(incident)
    await db.commit()
    await db.refresh(incident)
    return incident


@router.get("/", response_model=list[IncidentRead])
async def list_incidents(
    server_handle: str | None = Query(None, description="Filter by server handle"),
    status: str | None = Query(None, description="Filter by status"),
    incident_type: str | None = Query(None, description="Filter by incident type"),
    db: AsyncSession = Depends(get_async_session),
) -> list[Incident]:
    """List all incidents with optional filtering."""
    query = select(Incident)

    if server_handle is not None:
        query = query.where(Incident.server_handle == server_handle)

    if status is not None:
        query = query.where(Incident.status == status)

    if incident_type is not None:
        query = query.where(Incident.incident_type == incident_type)

    # Order by most recent first
    query = query.order_by(Incident.detected_at.desc())

    result = await db.execute(query)
    return result.scalars().all()


@router.get("/active", response_model=list[IncidentRead])
async def list_active_incidents(
    db: AsyncSession = Depends(get_async_session),
) -> list[Incident]:
    """Get all active incidents (detected or recovering)."""
    query = (
        select(Incident)
        .where(
            Incident.status.in_([IncidentStatus.DETECTED.value, IncidentStatus.RECOVERING.value])
        )
        .order_by(Incident.detected_at.desc())
    )

    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{incident_id}", response_model=IncidentRead)
async def get_incident(
    incident_id: int,
    db: AsyncSession = Depends(get_async_session),
) -> Incident:
    """Get incident by ID."""
    incident = await db.get(Incident, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


@router.patch("/{incident_id}", response_model=IncidentRead)
async def update_incident(
    incident_id: int,
    incident_update: IncidentUpdate,
    db: AsyncSession = Depends(get_async_session),
) -> Incident:
    """Update incident status and details."""
    incident = await db.get(Incident, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")

    # Update fields if provided
    if incident_update.status is not None:
        incident.status = incident_update.status

    if incident_update.resolved_at is not None:
        incident.resolved_at = incident_update.resolved_at

    if incident_update.details is not None:
        incident.details = incident_update.details

    if incident_update.recovery_attempts is not None:
        incident.recovery_attempts = incident_update.recovery_attempts

    await db.commit()
    await db.refresh(incident)
    return incident

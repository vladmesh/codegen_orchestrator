"""Service deployments router."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_async_session
from ..models import ServiceDeployment
from ..schemas import ServiceDeploymentCreate, ServiceDeploymentRead, ServiceDeploymentUpdate

router = APIRouter(prefix="/service-deployments", tags=["service-deployments"])


@router.post("/", response_model=ServiceDeploymentRead, status_code=status.HTTP_201_CREATED)
async def create_service_deployment(
    deployment_in: ServiceDeploymentCreate,
    db: AsyncSession = Depends(get_async_session),
) -> ServiceDeployment:
    """Create a new service deployment record."""
    deployment = ServiceDeployment(
        project_id=deployment_in.project_id,
        service_name=deployment_in.service_name,
        server_handle=deployment_in.server_handle,
        port=deployment_in.port,
        status=deployment_in.status,
        deployment_info=deployment_in.deployment_info,
    )
    db.add(deployment)
    await db.commit()
    await db.refresh(deployment)
    return deployment


@router.get("/", response_model=list[ServiceDeploymentRead])
async def list_service_deployments(
    server_handle: str | None = Query(None, description="Filter by server handle"),
    project_id: str | None = Query(None, description="Filter by project ID"),
    status: str | None = Query(None, description="Filter by status"),
    db: AsyncSession = Depends(get_async_session),
) -> list[ServiceDeployment]:
    """List all service deployments with optional filtering."""
    query = select(ServiceDeployment)
    
    if server_handle is not None:
        query = query.where(ServiceDeployment.server_handle == server_handle)
    
    if project_id is not None:
        query = query.where(ServiceDeployment.project_id == project_id)
    
    if status is not None:
        query = query.where(ServiceDeployment.status == status)
    
    # Order by most recent first
    query = query.order_by(ServiceDeployment.deployed_at.desc())
    
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{deployment_id}", response_model=ServiceDeploymentRead)
async def get_service_deployment(
    deployment_id: int,
    db: AsyncSession = Depends(get_async_session),
) -> ServiceDeployment:
    """Get service deployment by ID."""
    deployment = await db.get(ServiceDeployment, deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Service deployment not found")
    return deployment


@router.patch("/{deployment_id}", response_model=ServiceDeploymentRead)
async def update_service_deployment(
    deployment_id: int,
    deployment_update: ServiceDeploymentUpdate,
    db: AsyncSession = Depends(get_async_session),
) -> ServiceDeployment:
    """Update service deployment status and info."""
    deployment = await db.get(ServiceDeployment, deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Service deployment not found")
    
    # Update fields if provided
    if deployment_update.status is not None:
        deployment.status = deployment_update.status
    
    if deployment_update.deployment_info is not None:
        deployment.deployment_info = deployment_update.deployment_info
    
    await db.commit()
    await db.refresh(deployment)
    return deployment


@router.delete("/{deployment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_service_deployment(
    deployment_id: int,
    db: AsyncSession = Depends(get_async_session),
):
    """Delete a service deployment record."""
    deployment = await db.get(ServiceDeployment, deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Service deployment not found")
    
    await db.delete(deployment)
    await db.commit()

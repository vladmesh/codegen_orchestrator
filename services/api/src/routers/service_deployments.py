"""Deployments router (formerly service_deployments)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Deployment

from ..database import get_async_session
from ..schemas import DeploymentCreate, DeploymentRead, DeploymentUpdate

router = APIRouter(prefix="/service-deployments", tags=["deployments"])


@router.post("/", response_model=DeploymentRead, status_code=status.HTTP_201_CREATED)
async def create_deployment(
    deployment_in: DeploymentCreate,
    db: AsyncSession = Depends(get_async_session),
) -> Deployment:
    """Create a new deployment record."""
    deployment = Deployment(
        application_id=deployment_in.application_id,
        project_id=deployment_in.project_id,
        service_name=deployment_in.service_name,
        server_handle=deployment_in.server_handle,
        port=deployment_in.port,
        result=deployment_in.result,
        deployment_info=deployment_in.deployment_info,
        deployed_sha=deployment_in.deployed_sha,
    )
    db.add(deployment)
    await db.commit()
    await db.refresh(deployment)
    return deployment


@router.get("/", response_model=list[DeploymentRead])
async def list_deployments(
    server_handle: str | None = Query(None, description="Filter by server handle"),
    project_id: uuid.UUID | None = Query(None, description="Filter by project ID"),
    application_id: int | None = Query(None, description="Filter by application ID"),
    result: str | None = Query(None, description="Filter by result"),
    db: AsyncSession = Depends(get_async_session),
) -> list[Deployment]:
    """List all deployments with optional filtering."""
    query = select(Deployment)

    if server_handle is not None:
        query = query.where(Deployment.server_handle == server_handle)
    if project_id is not None:
        query = query.where(Deployment.project_id == project_id)
    if application_id is not None:
        query = query.where(Deployment.application_id == application_id)
    if result is not None:
        query = query.where(Deployment.result == result)

    query = query.order_by(Deployment.deployed_at.desc())
    result_set = await db.execute(query)
    return result_set.scalars().all()


@router.get("/{deployment_id}", response_model=DeploymentRead)
async def get_deployment(
    deployment_id: int,
    db: AsyncSession = Depends(get_async_session),
) -> Deployment:
    """Get deployment by ID."""
    deployment = await db.get(Deployment, deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return deployment


@router.patch("/{deployment_id}", response_model=DeploymentRead)
async def update_deployment(
    deployment_id: int,
    deployment_update: DeploymentUpdate,
    db: AsyncSession = Depends(get_async_session),
) -> Deployment:
    """Update deployment result and info."""
    deployment = await db.get(Deployment, deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    if deployment_update.result is not None:
        deployment.result = deployment_update.result
    if deployment_update.deployment_info is not None:
        deployment.deployment_info = deployment_update.deployment_info
    if deployment_update.deployed_sha is not None:
        deployment.deployed_sha = deployment_update.deployed_sha

    await db.commit()
    await db.refresh(deployment)
    return deployment


@router.delete("/{deployment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_deployment(
    deployment_id: int,
    db: AsyncSession = Depends(get_async_session),
):
    """Delete a deployment record."""
    deployment = await db.get(Deployment, deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    await db.delete(deployment)
    await db.commit()

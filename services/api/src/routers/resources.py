"""Resources router - CRUD for secrets handles."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_async_session as get_db
from ..models import Resource

router = APIRouter(tags=["resources"])


class ResourceCreate(BaseModel):
    """Resource creation request."""

    handle: str
    resource_type: str
    name: str
    metadata: dict | None = None


class ResourceRead(BaseModel):
    """Resource response."""

    handle: str
    resource_type: str
    name: str
    metadata: dict | None

    model_config = {"from_attributes": True}


@router.get("/resources/{handle}", response_model=ResourceRead)
async def get_resource(handle: str, db: AsyncSession = Depends(get_db)) -> Resource:
    """Get resource by handle."""
    result = await db.execute(select(Resource).where(Resource.handle == handle))
    resource = result.scalar_one_or_none()
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")
    return resource


@router.post("/resources", response_model=ResourceRead)
async def create_resource(data: ResourceCreate, db: AsyncSession = Depends(get_db)) -> Resource:
    """Create new resource."""
    resource = Resource(
        handle=data.handle,
        resource_type=data.resource_type,
        name=data.name,
        metadata_=data.metadata or {},
    )
    db.add(resource)
    await db.flush()
    return resource


@router.get("/resources", response_model=list[ResourceRead])
async def list_resources(
    resource_type: str | None = None, db: AsyncSession = Depends(get_db)
) -> list[Resource]:
    """List resources, optionally filtered by type."""
    query = select(Resource)
    if resource_type:
        query = query.where(Resource.resource_type == resource_type)
    result = await db.execute(query)
    return list(result.scalars().all())

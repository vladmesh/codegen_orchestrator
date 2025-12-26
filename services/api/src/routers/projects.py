"""Projects router."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.models import Project

from ..database import get_async_session
from ..schemas import ProjectCreate, ProjectRead, ProjectUpdate

logger = structlog.get_logger()

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("/", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
async def create_project(
    project_in: ProjectCreate,
    db: AsyncSession = Depends(get_async_session),
) -> Project:
    """Create a new project."""
    logger.info("creating_project", project_id=project_in.id, name=project_in.name)

    # Check if ID exists
    if await db.get(Project, project_in.id):
        logger.warning("project_creation_failed_duplicate", project_id=project_in.id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project with this ID already exists",
        )

    project = Project(
        id=project_in.id,
        name=project_in.name,
        status=project_in.status,
        config=project_in.config,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)

    logger.info(
        "project_created",
        project_id=project.id,
        status=project.status,
        config_keys=list(project.config.keys()) if project.config else [],
    )

    return project


@router.get("/{project_id}", response_model=ProjectRead)
async def get_project(
    project_id: str,
    db: AsyncSession = Depends(get_async_session),
) -> Project:
    """Get project by ID."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/", response_model=list[ProjectRead])
async def list_projects(
    status: str | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> list[Project]:
    """List projects, optionally filtered by status."""
    query = select(Project)
    if status:
        query = query.where(Project.status == status)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.put("/{project_id}", response_model=ProjectRead)
async def update_project(
    project_id: str,
    project_in: ProjectUpdate,
    db: AsyncSession = Depends(get_async_session),
) -> Project:
    """Update project."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if project_in.status is not None:
        project.status = project_in.status
    if project_in.config is not None:
        project.config = project_in.config

    await db.commit()
    await db.refresh(project)

    logger.info("project_updated", project_id=project.id, status=project.status)

    return project


@router.patch("/{project_id}", response_model=ProjectRead)
async def patch_project(
    project_id: str,
    project_in: ProjectUpdate,
    db: AsyncSession = Depends(get_async_session),
) -> Project:
    """Partial update of project (PATCH method)."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if project_in.status is not None:
        project.status = project_in.status
    if project_in.config is not None:
        project.config = project_in.config

    await db.commit()
    await db.refresh(project)

    logger.info("project_patched", project_id=project.id, status=project.status)

    return project

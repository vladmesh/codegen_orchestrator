"""Projects router."""

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.models import Project, User

from ..database import get_async_session
from ..schemas import ProjectCreate, ProjectRead, ProjectUpdate

logger = structlog.get_logger()

router = APIRouter(prefix="/projects", tags=["projects"])


async def _resolve_user(
    telegram_id: int | None,
    db: AsyncSession,
) -> User | None:
    """Resolve User from telegram_id."""
    if not telegram_id:
        return None
    query = select(User).where(User.telegram_id == telegram_id)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def _check_project_access(
    project: Project,
    telegram_id: int | None,
    db: AsyncSession,
) -> None:
    """Check if user has access to project. Raises 403 if denied."""
    if telegram_id is None:
        return  # No auth header - allow (backward compat for internal calls)

    user = await _resolve_user(telegram_id, db)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with telegram_id {telegram_id} not found",
        )

    if user.is_admin:
        return

    # Regular user: must be owner; unowned projects are admin-only
    if project.owner_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: not project owner",
        )


@router.post("/", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
async def create_project(
    request: Request,
    project_in: ProjectCreate,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
) -> Project:
    """Create a new project."""
    try:
        logger.info(
            "creating_project",
            project_id=project_in.id,
            name=project_in.name,
            status=project_in.status,
            config=project_in.config,
            telegram_id=x_telegram_id,
        )

        # Check if ID exists
        if await db.get(Project, project_in.id):
            logger.warning("project_creation_failed_duplicate", project_id=project_in.id)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Project with this ID already exists",
            )

        # Resolve owner
        owner_id = None
        if x_telegram_id:
            user = await _resolve_user(x_telegram_id, db)
            if user:
                owner_id = user.id

        project = Project(
            id=project_in.id,
            name=project_in.name,
            status=project_in.status,
            config=project_in.config,
            owner_id=owner_id,
            github_repo_id=project_in.github_repo_id,
        )
        db.add(project)
        await db.commit()
        await db.refresh(project)

        logger.info(
            "project_created",
            project_id=project.id,
            status=project.status,
            owner_id=owner_id,
        )

        return project

    except HTTPException:
        raise
    except Exception as e:
        # Log validation or other errors with full request details
        try:
            body = await request.body()
            body_str = body.decode("utf-8") if body else "empty"
        except Exception:
            body_str = "unable to read"

        logger.error(
            "project_creation_failed",
            error=str(e),
            error_type=type(e).__name__,
            request_body=body_str,
            telegram_id=x_telegram_id,
        )
        raise


@router.get("/{project_id}", response_model=ProjectRead)
async def get_project(
    project_id: str,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
) -> Project:
    """Get project by ID."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await _check_project_access(project, x_telegram_id, db)
    return project


@router.get("/", response_model=list[ProjectRead])
async def list_projects(
    status: str | None = None,
    owner_only: bool = False,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
) -> list[Project]:
    """List projects, optionally filtered by status."""
    query = select(Project)

    # Filter by owner if user provided and not admin, or if owner_only requested
    if x_telegram_id is not None:
        user = await _resolve_user(x_telegram_id, db)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with telegram_id {x_telegram_id} not found",
            )
        if not user.is_admin or owner_only:
            # Regular user or explict owner_only request: only their projects
            query = query.where(Project.owner_id == user.id)

    if status:
        query = query.where(Project.status == status)

    result = await db.execute(query)
    return list(result.scalars().all())


@router.put("/{project_id}", response_model=ProjectRead)
async def update_project(
    project_id: str,
    project_in: ProjectUpdate,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
) -> Project:
    """Update project."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await _check_project_access(project, x_telegram_id, db)

    if project_in.status is not None:
        project.status = project_in.status
    if project_in.config is not None:
        project.config = project_in.config
    if project_in.repository_url is not None:
        project.repository_url = project_in.repository_url
    if project_in.github_repo_id is not None:
        project.github_repo_id = project_in.github_repo_id

    await db.commit()
    await db.refresh(project)

    logger.info("project_patched", project_id=project.id, status=project.status)

    return project


@router.patch("/{project_id}", response_model=ProjectRead)
async def patch_project(
    project_id: str,
    project_in: ProjectUpdate,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
) -> Project:
    """Partial update of project (PATCH method)."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await _check_project_access(project, x_telegram_id, db)

    if project_in.status is not None:
        project.status = project_in.status
    if project_in.config is not None:
        project.config = project_in.config
    if project_in.repository_url is not None:
        project.repository_url = project_in.repository_url
    if project_in.github_repo_id is not None:
        project.github_repo_id = project_in.github_repo_id

    await db.commit()
    await db.refresh(project)

    logger.info("project_patched", project_id=project.id, status=project.status)

    return project

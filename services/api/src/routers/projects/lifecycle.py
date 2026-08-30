"""Project lifecycle and metadata routes."""

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.work_admission import WorkAdmissionOutcome
from shared.contracts.vocab import AgentType
from shared.models import Project, Repository
from shared.project_slug import generate_project_slug

from ...config import get_settings
from ...database import get_async_session
from ...dependencies import _optional_bearer_scheme, is_internal_service, resolve_actor
from ...schemas import ProjectCreate, ProjectRead, ProjectUpdate
from ...utils.telegram_binding import release_bot_binding
from ...work_admission import admit_project_creation
from ..projects_guards import check_project_access, load_locked_project
from .secrets import _vet_config_write

logger = structlog.get_logger()
router = APIRouter()


def _initial_project_config(config: dict) -> dict:
    """Resolve the developer agent exactly once, when a project is created."""
    resolved = dict(config)
    explicit_agent_type = resolved.get("agent_type")
    if explicit_agent_type is None:
        resolved["agent_type"] = get_settings().default_agent_type.value
        return resolved

    try:
        resolved["agent_type"] = AgentType(explicit_agent_type).value
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unsupported agent_type: {explicit_agent_type}",
        ) from exc
    return resolved


@router.post("/", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
async def create_project(
    request: Request,
    project_in: ProjectCreate,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> Project:
    """Create a new project."""
    try:
        project_id = project_in.id or uuid.uuid4()

        logger.info(
            "creating_project",
            project_id=str(project_id),
            title=project_in.title,
            status=project_in.status,
            telegram_id=x_telegram_id,
        )

        # Check if ID exists
        if project_in.id and await db.get(Project, project_id):
            logger.warning("project_creation_failed_duplicate", project_id=str(project_id))
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Project with this ID already exists",
            )

        # Ownership and admission are both derived from the caller principal.
        # A bearer token names its subject; the Telegram header names an actor
        # only for an internal service caller.
        user = await resolve_actor(
            is_internal=_is_internal,
            telegram_id=x_telegram_id,
            credentials=credentials,
            db=db,
        )
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X-Telegram-ID header is required",
            )
        owner_id = user.id

        admission = await admit_project_creation(user.id, user.is_admin, db)
        if admission.outcome is not WorkAdmissionOutcome.ADMITTED:
            await db.commit()
            logger.info(
                "project_creation_admission_denied",
                owner_id=user.id,
                reason=admission.reason.value if admission.reason else None,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"admission": admission.model_dump(mode="json")},
            )

        project = Project(
            id=project_id,
            title=project_in.title,
            slug=generate_project_slug(project_in.title, project_id),
            status=project_in.status.value,
            config=_vet_config_write(_initial_project_config(project_in.config), None),
            owner_id=owner_id,
            # Ownership enters the system here and nowhere else: the caller that
            # started the run names it, and every worker created for this
            # project is stamped with it later.
            initiating_run_id=project_in.initiating_run_id,
        )
        db.add(project)
        await db.commit()
        await db.refresh(project)

        logger.info(
            "project_created",
            project_id=str(project.id),
            status=project.status,
            owner_id=owner_id,
        )

        return project

    except HTTPException:
        raise
    except Exception as e:
        # The creation body carries project secrets, so it never reaches the log
        # stream. Its size is enough to tell an empty request from a truncated one.
        try:
            body_size = len(await request.body())
        except Exception:
            body_size = -1

        logger.error(
            "project_creation_failed",
            error=str(e),
            error_type=type(e).__name__,
            request_body_bytes=body_size,
            telegram_id=x_telegram_id,
        )
        raise


@router.get("/{project_id}", response_model=ProjectRead)
async def get_project(
    project_id: uuid.UUID,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> Project:
    """Get project by ID."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await check_project_access(
        project,
        x_telegram_id,
        db,
        is_internal=_is_internal,
        credentials=credentials,
    )
    return project


@router.get("/", response_model=list[ProjectRead])
async def list_projects(
    # alias keeps the public query param name; a parameter literally named
    # `status` would shadow the fastapi.status module used below
    project_status: str | None = Query(None, alias="status"),
    owner_id: int | None = None,
    owner_only: bool = False,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> list[Project]:
    """List projects, optionally filtered by status or owner_id."""
    # The caller is resolved before any filter is applied, including the admin
    # panel's owner_id filter: passing owner_id must not be a way to read another
    # user's projects and their config.
    actor = await resolve_actor(
        is_internal=_is_internal,
        telegram_id=x_telegram_id,
        credentials=credentials,
        db=db,
    )

    query = select(Project)

    if actor is None:
        # Service call: no user to scope against.
        if owner_id is not None:
            query = query.where(Project.owner_id == owner_id)
    elif actor.is_admin:
        if owner_id is not None:
            query = query.where(Project.owner_id == owner_id)
        elif owner_only:
            query = query.where(Project.owner_id == actor.id)
    else:
        # A regular user sees only their own projects, whatever owner_id says.
        query = query.where(Project.owner_id == actor.id)

    if project_status:
        query = query.where(Project.status == project_status)

    result = await db.execute(query)
    return list(result.scalars().all())


async def _release_bot_if_archived(db: AsyncSession, project: Project) -> None:
    """Archiving is teardown, so the project stops holding its bot.

    Keyed on the resulting status, not on the transition: archiving an already
    archived project releases nothing the first call left behind.
    """
    if project.status != ProjectStatus.ARCHIVED.value:
        return
    repos = await db.execute(select(Repository).where(Repository.project_id == project.id))
    await release_bot_binding(db, project, repos.scalars().all(), reason="project_archived")


@router.put("/{project_id}", response_model=ProjectRead)
async def update_project(
    project_id: uuid.UUID,
    project_in: ProjectUpdate,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> Project:
    """Update project."""
    project = await load_locked_project(db, project_id)

    await check_project_access(
        project,
        x_telegram_id,
        db,
        is_internal=_is_internal,
        credentials=credentials,
    )

    if project_in.title is not None:
        project.title = project_in.title
    if project_in.status is not None:
        project.status = project_in.status.value
    if project_in.config is not None:
        project.config = _vet_config_write(project_in.config, project)
    if project_in.project_spec is not None:
        project.project_spec = project_in.project_spec

    await _release_bot_if_archived(db, project)

    await db.commit()
    await db.refresh(project)

    logger.info("project_patched", project_id=str(project.id), status=project.status)

    return project


@router.patch("/{project_id}", response_model=ProjectRead)
async def patch_project(
    project_id: uuid.UUID,
    project_in: ProjectUpdate,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> Project:
    """Partial update of project (PATCH method)."""
    project = await load_locked_project(db, project_id)

    await check_project_access(
        project,
        x_telegram_id,
        db,
        is_internal=_is_internal,
        credentials=credentials,
    )

    if project_in.title is not None:
        project.title = project_in.title
    if project_in.status is not None:
        project.status = project_in.status.value
    if project_in.config is not None:
        project.config = _vet_config_write(project_in.config, project)
    if project_in.project_spec is not None:
        project.project_spec = project_in.project_spec

    await _release_bot_if_archived(db, project)

    await db.commit()
    await db.refresh(project)

    logger.info("project_patched", project_id=str(project.id), status=project.status)

    return project

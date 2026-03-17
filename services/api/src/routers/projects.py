"""Projects router."""

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
import redis.asyncio as aioredis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.project import ProjectStatus
from shared.crypto import decrypt_dict, encrypt_dict
from shared.models import Application, PortAllocation, Project, Repository, Run, User
from shared.queues import ARCHITECT_QUEUE, DEPLOY_QUEUE, ENGINEERING_QUEUE, SCAFFOLD_QUEUE

from ..config import get_settings
from ..database import get_async_session
from ..schemas import MergeSecretsRequest, ProjectCreate, ProjectRead, ProjectUpdate

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
        project_id = project_in.id or uuid.uuid4()

        logger.info(
            "creating_project",
            project_id=str(project_id),
            name=project_in.name,
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

        # Resolve owner — required
        if not x_telegram_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X-Telegram-ID header is required",
            )
        user = await _resolve_user(x_telegram_id, db)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with telegram_id {x_telegram_id} not found",
            )
        owner_id = user.id

        project = Project(
            id=project_id,
            name=project_in.name,
            status=project_in.status or ProjectStatus.DRAFT.value,
            config=project_in.config,
            owner_id=owner_id,
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
    project_id: uuid.UUID,
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
    owner_id: int | None = None,
    owner_only: bool = False,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
) -> list[Project]:
    """List projects, optionally filtered by status or owner_id."""
    query = select(Project)

    # Direct owner_id filter (from admin panel)
    if owner_id is not None:
        query = query.where(Project.owner_id == owner_id)

    # Filter by owner if user provided and not admin, or if owner_only requested
    elif x_telegram_id is not None:
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
    project_id: uuid.UUID,
    project_in: ProjectUpdate,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
) -> Project:
    """Update project."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await _check_project_access(project, x_telegram_id, db)

    if project_in.name is not None:
        project.name = project_in.name
    if project_in.status is not None:
        project.status = project_in.status
    if project_in.config is not None:
        project.config = project_in.config

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
) -> Project:
    """Partial update of project (PATCH method)."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await _check_project_access(project, x_telegram_id, db)

    if project_in.name is not None:
        project.name = project_in.name
    if project_in.status is not None:
        project.status = project_in.status
    if project_in.config is not None:
        project.config = project_in.config

    await db.commit()
    await db.refresh(project)

    logger.info("project_patched", project_id=str(project.id), status=project.status)

    return project


@router.post("/{project_id}/config/secrets")
async def merge_secrets(
    project_id: uuid.UUID,
    body: MergeSecretsRequest,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
) -> dict:
    """Atomically merge secrets into project config.

    Uses SELECT FOR UPDATE to prevent race conditions when multiple
    callers set secrets concurrently.
    """
    if not body.secrets:
        raise HTTPException(
            status_code=422,
            detail="secrets must not be empty",
        )

    # Lock the row to prevent concurrent read-modify-write
    query = select(Project).where(Project.id == project_id).with_for_update()
    result = await db.execute(query)
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await _check_project_access(project, x_telegram_id, db)

    config = dict(project.config or {})
    existing_secrets = config.get("secrets") or {}
    existing_secrets = decrypt_dict(existing_secrets) if existing_secrets else {}

    existing_secrets.update(body.secrets)
    config["secrets"] = encrypt_dict(existing_secrets)

    if body.env_hints:
        env_hints = config.get("env_hints") or {}
        env_hints.update(body.env_hints)
        config["env_hints"] = env_hints

    project.config = config
    await db.commit()

    logger.info(
        "secrets_merged",
        project_id=project_id,
        keys=sorted(body.secrets.keys()),
    )

    return {"keys": sorted(existing_secrets.keys())}


_QUEUES_TO_CLEAN = [ARCHITECT_QUEUE, SCAFFOLD_QUEUE, ENGINEERING_QUEUE, DEPLOY_QUEUE]


async def _cleanup_project_queue_messages(project_id: str) -> int:
    """Remove stale queue messages referencing a deleted project.

    Scans all pipeline queues and deletes messages whose project_id matches.
    Best-effort — failures are logged but don't block project deletion.
    """
    settings = get_settings()
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    deleted = 0
    try:
        for queue in _QUEUES_TO_CLEAN:
            try:
                entries = await r.xrange(queue)
            except Exception as exc:
                logger.debug("queue_scan_failed", queue=queue, error=str(exc))
                continue
            for entry_id, fields in entries:
                if fields.get("project_id") == project_id:
                    await r.xdel(queue, entry_id)
                    deleted += 1
        # Also clear scaffold inflight marker
        await r.delete(f"scaffold:inflight:{project_id}")
    finally:
        await r.aclose()
    if deleted:
        logger.info("project_queue_messages_cleaned", project_id=project_id, deleted=deleted)
    return deleted


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID,
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    db: AsyncSession = Depends(get_async_session),
):
    """Delete a project and its related records (tasks, port allocations)."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await _check_project_access(project, x_telegram_id, db)

    # Delete FK-constrained related records
    await db.execute(delete(Run).where(Run.project_id == project_id))

    # Delete port allocations and applications via project's repositories
    repo_ids_q = select(Repository.id).where(Repository.project_id == project_id)
    app_ids_q = select(Application.id).where(Application.repo_id.in_(repo_ids_q))
    await db.execute(delete(PortAllocation).where(PortAllocation.application_id.in_(app_ids_q)))
    await db.execute(delete(Application).where(Application.repo_id.in_(repo_ids_q)))

    await db.delete(project)
    await db.commit()

    # Best-effort cleanup: remove stale queue messages for this project
    try:
        await _cleanup_project_queue_messages(str(project_id))
    except Exception as e:
        logger.warning("project_queue_cleanup_failed", project_id=project_id, error=str(e))

    logger.info("project_deleted", project_id=project_id)

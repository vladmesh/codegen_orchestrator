"""Repositories router — CRUD for git repositories linked to projects."""

from datetime import UTC, datetime
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.models.repository import Repository

from ..database import get_async_session
from ..schemas.repository import RepositoryCreate, RepositoryRead, RepositoryUpdate

logger = structlog.get_logger()

router = APIRouter(prefix="/repositories", tags=["repositories"])


def _generate_id() -> str:
    return f"repo-{secrets.token_hex(4)}"


async def _get_repository(repo_id: str, db: AsyncSession) -> Repository:
    query = select(Repository).where(Repository.id == repo_id)
    result = await db.execute(query)
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Repository {repo_id} not found",
        )
    return repo


# --- CRUD ---


@router.post("/", response_model=RepositoryRead, status_code=status.HTTP_201_CREATED)
async def create_repository(
    body: RepositoryCreate,
    db: AsyncSession = Depends(get_async_session),
) -> RepositoryRead:
    now = datetime.now(UTC)
    repo = Repository(
        id=_generate_id(),
        project_id=body.project_id,
        name=body.name,
        git_url=body.git_url,
        provider_repo_id=body.provider_repo_id,
        role=body.role.value,
        is_managed=body.is_managed,
        created_at=now,
        updated_at=now,
    )
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    logger.info("repository_created", repository_id=repo.id, name=repo.name)
    return RepositoryRead.model_validate(repo, from_attributes=True)


@router.get("/", response_model=list[RepositoryRead])
async def list_repositories(
    project_id: str | None = None,
    role: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_async_session),
) -> list[RepositoryRead]:
    query = select(Repository)

    if project_id:
        query = query.where(Repository.project_id == project_id)
    if role:
        query = query.where(Repository.role == role)

    query = query.order_by(Repository.created_at.desc()).limit(limit)

    result = await db.execute(query)
    items = result.scalars().all()
    return [RepositoryRead.model_validate(r, from_attributes=True) for r in items]


@router.get("/by-provider-id/{provider_repo_id}", response_model=RepositoryRead)
async def get_repository_by_provider_id(
    provider_repo_id: int,
    db: AsyncSession = Depends(get_async_session),
) -> RepositoryRead:
    query = select(Repository).where(Repository.provider_repo_id == provider_repo_id)
    result = await db.execute(query)
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Repository with provider_repo_id={provider_repo_id} not found",
        )
    return RepositoryRead.model_validate(repo, from_attributes=True)


@router.get("/{repo_id}", response_model=RepositoryRead)
async def get_repository(
    repo_id: str,
    db: AsyncSession = Depends(get_async_session),
) -> RepositoryRead:
    repo = await _get_repository(repo_id, db)
    return RepositoryRead.model_validate(repo, from_attributes=True)


@router.patch("/{repo_id}", response_model=RepositoryRead)
async def update_repository(
    repo_id: str,
    body: RepositoryUpdate,
    db: AsyncSession = Depends(get_async_session),
) -> RepositoryRead:
    repo = await _get_repository(repo_id, db)

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(repo, field, value)

    await db.commit()
    await db.refresh(repo)

    logger.info("repository_updated", repository_id=repo.id, fields=list(update_data.keys()))
    return RepositoryRead.model_validate(repo, from_attributes=True)


@router.delete("/{repo_id}", response_model=RepositoryRead)
async def delete_repository(
    repo_id: str,
    db: AsyncSession = Depends(get_async_session),
) -> RepositoryRead:
    repo = await _get_repository(repo_id, db)
    await db.delete(repo)
    await db.commit()

    logger.info("repository_deleted", repository_id=repo.id)
    return RepositoryRead.model_validate(repo, from_attributes=True)

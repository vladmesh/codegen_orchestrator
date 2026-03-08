"""Repository API schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from shared.contracts.dto.repository import RepositoryRole


class RepositoryCreate(BaseModel):
    """Schema for creating a repository."""

    project_id: str
    name: str
    git_url: str
    provider_repo_id: int | None = None
    role: RepositoryRole = RepositoryRole.PRIMARY
    is_managed: bool = True


class RepositoryRead(BaseModel):
    """Schema for reading a repository."""

    id: str
    project_id: str
    name: str
    git_url: str
    provider_repo_id: int | None
    role: str
    is_managed: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RepositoryUpdate(BaseModel):
    """Schema for updating a repository."""

    name: str | None = None
    git_url: str | None = None
    provider_repo_id: int | None = None
    role: RepositoryRole | None = None
    is_managed: bool | None = None

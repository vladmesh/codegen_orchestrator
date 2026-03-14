"""Repository API schemas."""

import uuid

from pydantic import BaseModel, ConfigDict

from shared.contracts.dto.base import TimestampedDTO
from shared.contracts.dto.repository import RepositoryRole, RepositoryVisibility


class RepositoryCreate(BaseModel):
    """Schema for creating a repository."""

    project_id: uuid.UUID
    name: str
    git_url: str
    provider_repo_id: int | None = None
    role: RepositoryRole = RepositoryRole.PRIMARY
    visibility: RepositoryVisibility = RepositoryVisibility.PRIVATE
    is_managed: bool = True


class RepositoryRead(TimestampedDTO):
    """Schema for reading a repository."""

    id: str
    project_id: uuid.UUID
    name: str
    git_url: str
    provider_repo_id: int | None
    role: str
    visibility: str
    is_managed: bool

    model_config = ConfigDict(from_attributes=True)


class RepositoryUpdate(BaseModel):
    """Schema for updating a repository."""

    name: str | None = None
    git_url: str | None = None
    provider_repo_id: int | None = None
    role: RepositoryRole | None = None
    visibility: RepositoryVisibility | None = None
    is_managed: bool | None = None

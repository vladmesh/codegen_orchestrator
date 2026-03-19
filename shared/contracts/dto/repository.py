"""Repository DTOs and enums."""

from enum import StrEnum
import uuid

from pydantic import BaseModel

from shared.contracts.dto.base import TimestampedDTO


class RepositoryRole(StrEnum):
    PRIMARY = "primary"
    DEPENDENCY = "dependency"


class RepositoryVisibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


class RepositoryStatus(StrEnum):
    """Whether the repository is accessible."""

    ACTIVE = "active"
    MISSING = "missing"


# --- Response DTOs ---


class RepositoryDTO(TimestampedDTO):
    """Repository response from API."""

    id: str
    project_id: uuid.UUID
    name: str
    git_url: str
    provider_repo_id: int | None = None
    role: str
    visibility: str
    is_managed: bool


# --- Request DTOs ---


class RepositoryCreate(BaseModel):
    """Create repository request."""

    project_id: uuid.UUID
    name: str
    git_url: str
    provider_repo_id: int | None = None
    role: RepositoryRole = RepositoryRole.PRIMARY
    visibility: RepositoryVisibility = RepositoryVisibility.PRIVATE
    is_managed: bool = True


class RepositoryUpdate(BaseModel):
    """Update repository request."""

    name: str | None = None
    git_url: str | None = None
    provider_repo_id: int | None = None
    role: RepositoryRole | None = None
    visibility: RepositoryVisibility | None = None
    is_managed: bool | None = None

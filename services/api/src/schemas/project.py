"""Project schemas."""

from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict

from shared.contracts.dto.base import TimestampedDTO
from shared.contracts.dto.project import ProjectStatus, ServiceModule


class ProjectBase(BaseModel):
    """Base project schema."""

    id: uuid.UUID
    title: str
    slug: str
    status: str = ProjectStatus.DRAFT.value
    config: dict[str, Any] = {}


class ProjectCreate(BaseModel):
    """Schema for creating a project."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None  # Auto-generated if not provided
    title: str
    status: str = ProjectStatus.DRAFT.value
    config: dict[str, Any] = {}
    modules: list[ServiceModule] = [ServiceModule.BACKEND]


class ProjectRead(ProjectBase, TimestampedDTO):
    """Schema for reading a project."""

    model_config = ConfigDict(from_attributes=True)

    owner_id: int


class ProjectUpdate(BaseModel):
    """Schema for updating a project."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    status: str | None = None
    config: dict[str, Any] | None = None


class MergeSecretsRequest(BaseModel):
    """Schema for atomic secret merge."""

    secrets: dict[str, str]
    env_hints: dict[str, str] | None = None

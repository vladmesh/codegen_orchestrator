"""Project schemas."""

from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, field_validator

from shared.contracts.dto.base import TimestampedDTO
from shared.contracts.dto.project import ProjectStatus, ServiceModule
from shared.contracts.runtime_project import runtime_project_slug


class ProjectBase(BaseModel):
    """Base project schema."""

    id: uuid.UUID
    name: str
    status: str = ProjectStatus.DRAFT.value
    config: dict[str, Any] = {}

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return str(runtime_project_slug(value))


class ProjectCreate(ProjectBase):
    """Schema for creating a project."""

    id: uuid.UUID | None = None  # Auto-generated if not provided
    modules: list[ServiceModule] = [ServiceModule.BACKEND]


class ProjectRead(ProjectBase, TimestampedDTO):
    """Schema for reading a project."""

    model_config = ConfigDict(from_attributes=True)

    owner_id: int


class ProjectUpdate(BaseModel):
    """Schema for updating a project."""

    name: str | None = None
    status: str | None = None
    config: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return str(runtime_project_slug(value))


class MergeSecretsRequest(BaseModel):
    """Schema for atomic secret merge."""

    secrets: dict[str, str]
    env_hints: dict[str, str] | None = None

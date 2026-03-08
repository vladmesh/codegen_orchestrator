"""Project schemas."""

from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict

from shared.contracts.dto.project import ServiceModule


class ProjectBase(BaseModel):
    """Base project schema."""

    id: uuid.UUID
    name: str
    status: str = "draft"
    config: dict[str, Any] = {}


class ProjectCreate(ProjectBase):
    """Schema for creating a project."""

    id: uuid.UUID | None = None  # Auto-generated if not provided
    modules: list[ServiceModule] = [ServiceModule.BACKEND]


class ProjectRead(ProjectBase):
    """Schema for reading a project."""

    model_config = ConfigDict(from_attributes=True)

    owner_id: int


class ProjectUpdate(BaseModel):
    """Schema for updating a project."""

    name: str | None = None
    status: str | None = None
    config: dict[str, Any] | None = None


class MergeSecretsRequest(BaseModel):
    """Schema for atomic secret merge."""

    secrets: dict[str, str]
    env_hints: dict[str, str] | None = None

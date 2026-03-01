"""Project schemas."""

from typing import Any

from pydantic import BaseModel, ConfigDict

from shared.contracts.dto.project import ServiceModule


class ProjectBase(BaseModel):
    """Base project schema."""

    id: str
    name: str
    github_repo_id: int | None = None
    status: str = "created"
    config: dict[str, Any] = {}
    repository_url: str | None = None


class ProjectCreate(ProjectBase):
    """Schema for creating a project."""

    modules: list[ServiceModule] = [ServiceModule.BACKEND]


class ProjectRead(ProjectBase):
    """Schema for reading a project."""

    model_config = ConfigDict(from_attributes=True)


class ProjectUpdate(BaseModel):
    """Schema for updating a project."""

    status: str | None = None
    config: dict[str, Any] | None = None
    repository_url: str | None = None
    github_repo_id: int | None = None

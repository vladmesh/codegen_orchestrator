"""Project schemas."""

from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from shared.schemas.modules import ServiceModule


class ProjectBase(BaseModel):
    """Base project schema."""

    id: str
    name: str
    status: str = "created"
    config: dict[str, Any] = {}
    repository_url: str | None = None


class ProjectCreate(ProjectBase):
    """Schema for creating a project."""

    modules: list[ServiceModule] = [ServiceModule.BACKEND]

    @field_validator("modules")
    @classmethod
    def validate_backend_required(cls, v: list[ServiceModule]) -> list[ServiceModule]:
        """Ensure backend module is always included."""
        if ServiceModule.BACKEND not in v:
            v = [ServiceModule.BACKEND, *v]
        return v


class ProjectRead(ProjectBase):
    """Schema for reading a project."""

    model_config = ConfigDict(from_attributes=True)


class ProjectUpdate(BaseModel):
    """Schema for updating a project."""

    status: str | None = None
    config: dict[str, Any] | None = None
    repository_url: str | None = None

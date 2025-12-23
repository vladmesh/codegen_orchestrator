"""Project schemas."""

from pydantic import BaseModel, ConfigDict
from typing import Optional, Any


class ProjectBase(BaseModel):
    """Base project schema."""

    id: str
    name: str
    status: str = "created"
    config: dict[str, Any] = {}


class ProjectCreate(ProjectBase):
    """Schema for creating a project."""

    pass


class ProjectRead(ProjectBase):
    """Schema for reading a project."""

    model_config = ConfigDict(from_attributes=True)


class ProjectUpdate(BaseModel):
    """Schema for updating a project."""

    status: Optional[str] = None
    config: Optional[dict[str, Any]] = None

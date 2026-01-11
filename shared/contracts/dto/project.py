from enum import Enum

from pydantic import BaseModel, ConfigDict


class ProjectStatus(str, Enum):
    DRAFT = "draft"
    SCAFFOLDING = "scaffolding"
    SCAFFOLDED = "scaffolded"
    DEVELOPING = "developing"
    TESTING = "testing"
    DEPLOYING = "deploying"
    ACTIVE = "active"
    FAILED = "failed"
    ARCHIVED = "archived"


class ServiceModule(str, Enum):
    """Available project modules for scaffolding."""

    BACKEND = "backend"
    TELEGRAM = "telegram"
    FRONTEND = "frontend"


class ProjectCreate(BaseModel):
    """Create project request."""

    name: str
    description: str | None = None
    modules: list[ServiceModule] = [ServiceModule.BACKEND]  # Default: backend only


class ProjectDTO(BaseModel):
    """Project response."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str | None = None
    status: ProjectStatus
    modules: list[ServiceModule] = []
    repository_url: str | None = None
    owner_id: int | None = None

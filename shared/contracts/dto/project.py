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
    DISCOVERED = "discovered"
    MISSING = "missing"


class ServiceModule(str, Enum):
    """Available project modules for scaffolding."""

    BACKEND = "backend"
    TELEGRAM = "telegram"
    FRONTEND = "frontend"


class ProjectCreate(BaseModel):
    """Create project request."""

    id: str | None = None
    name: str
    description: str | None = None
    modules: list[ServiceModule] = [ServiceModule.BACKEND]  # Default: backend only
    github_repo_id: int | None = None
    status: ProjectStatus | None = None


class ProjectUpdate(BaseModel):
    """Update project request."""

    name: str | None = None
    description: str | None = None
    status: ProjectStatus | None = None
    modules: list[ServiceModule] | None = None
    github_repo_id: int | None = None
    owner_id: int | None = None
    project_spec: dict | None = None


class ProjectDTO(BaseModel):
    """Project response."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str | None = None
    status: ProjectStatus
    modules: list[ServiceModule] = []
    repository_url: str | None = None
    github_repo_id: int | None = None
    owner_id: int | None = None
    project_spec: dict | None = None

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ProjectStatus(StrEnum):
    """Project lifecycle status.

    Happy path: DRAFT → SCAFFOLDING → SCAFFOLDED → DEVELOPING → TESTING → DEPLOYING → ACTIVE
    """

    # Origin
    DRAFT = "draft"
    DISCOVERED = "discovered"

    # Scaffolding
    SCAFFOLDING = "scaffolding"
    SCAFFOLDED = "scaffolded"
    SCAFFOLD_FAILED = "scaffold_failed"

    # Development
    DEVELOPING = "developing"
    TESTING = "testing"

    # Deployment
    DEPLOYING = "deploying"
    ACTIVE = "active"

    # Maintenance
    MAINTENANCE = "maintenance"

    # Issues
    FAILED = "failed"
    MISSING = "missing"
    ARCHIVED = "archived"


class ServiceModule(StrEnum):
    """Available project modules for scaffolding.

    Must match module names in service-template/copier.yml.
    """

    BACKEND = "backend"
    TG_BOT = "tg_bot"
    NOTIFICATIONS = "notifications"
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

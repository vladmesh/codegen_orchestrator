from enum import StrEnum
import uuid

from pydantic import BaseModel, ConfigDict


class ProjectStatus(StrEnum):
    """Project lifecycle status.

    Happy path: DRAFT → SCAFFOLDING → SCAFFOLDED → DEVELOPING → TESTING → DEPLOYING → ACTIVE
    """

    # Origin
    DRAFT = "draft"

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
    ERROR = "error"
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

    id: uuid.UUID | None = None
    name: str
    description: str | None = None
    modules: list[ServiceModule] = [ServiceModule.BACKEND]  # Default: backend only
    status: ProjectStatus | None = None


class ProjectUpdate(BaseModel):
    """Update project request."""

    name: str | None = None
    description: str | None = None
    status: ProjectStatus | None = None
    modules: list[ServiceModule] | None = None
    project_spec: dict | None = None


class ProjectDTO(BaseModel):
    """Project response."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None = None
    status: ProjectStatus
    modules: list[ServiceModule] = []
    config: dict = {}
    owner_id: int
    project_spec: dict | None = None

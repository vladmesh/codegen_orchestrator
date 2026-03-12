from enum import StrEnum
import uuid

from pydantic import BaseModel, ConfigDict


class ProjectStatus(StrEnum):
    """Project lifecycle status.

    Lifecycle only — observable state, not process.
    Activity is derived from child entities (Story/Run).
    Runtime state is tracked by ServiceStatus.
    """

    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class ServiceStatus(StrEnum):
    """Runtime state of a project's deployed service."""

    NOT_DEPLOYED = "not_deployed"
    RUNNING = "running"
    DEGRADED = "degraded"
    DOWN = "down"
    STOPPED = "stopped"


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
    service_status: ServiceStatus | None = None


class ProjectUpdate(BaseModel):
    """Update project request."""

    name: str | None = None
    description: str | None = None
    status: ProjectStatus | None = None
    service_status: ServiceStatus | None = None
    modules: list[ServiceModule] | None = None
    project_spec: dict | None = None


class ProjectDTO(BaseModel):
    """Project response."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None = None
    status: ProjectStatus
    service_status: ServiceStatus = ServiceStatus.NOT_DEPLOYED
    modules: list[ServiceModule] = []
    config: dict = {}
    owner_id: int
    project_spec: dict | None = None

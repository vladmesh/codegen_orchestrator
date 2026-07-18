from enum import StrEnum
import uuid

from pydantic import BaseModel, field_validator

from shared.contracts.dto.base import TimestampedDTO
from shared.contracts.runtime_project import runtime_project_slug


class ProjectStatus(StrEnum):
    """Project lifecycle status.

    Lifecycle only — observable state, not process.
    Activity is derived from child entities (Story/Run).
    Runtime state is tracked by Application.status.
    """

    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
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

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return str(runtime_project_slug(value))


class ProjectUpdate(BaseModel):
    """Update project request."""

    name: str | None = None
    description: str | None = None
    status: ProjectStatus | None = None
    modules: list[ServiceModule] | None = None
    project_spec: dict | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return str(runtime_project_slug(value))


class ProjectDTO(TimestampedDTO):
    """Project response."""

    id: uuid.UUID
    name: str
    description: str | None = None
    status: ProjectStatus
    modules: list[ServiceModule] = []
    config: dict = {}
    owner_id: int
    project_spec: dict | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return str(runtime_project_slug(value))

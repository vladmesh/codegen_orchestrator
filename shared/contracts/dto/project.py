from enum import StrEnum
import uuid

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, computed_field

from shared.contracts.dto.base import TimestampedDTO


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

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    title: str = Field(validation_alias=AliasChoices("title", "name"))
    description: str | None = None
    modules: list[ServiceModule] = [ServiceModule.BACKEND]  # Default: backend only
    status: ProjectStatus | None = None


class ProjectUpdate(BaseModel):
    """Update project request."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, validation_alias=AliasChoices("title", "name"))
    description: str | None = None
    status: ProjectStatus | None = None
    modules: list[ServiceModule] | None = None
    project_spec: dict | None = None


class ProjectDTO(TimestampedDTO):
    """Project response."""

    id: uuid.UUID
    title: str = Field(validation_alias=AliasChoices("title", "name"))
    slug: str
    description: str | None = None
    status: ProjectStatus
    modules: list[ServiceModule] = []
    config: dict = {}
    owner_id: int
    project_spec: dict | None = None

    @computed_field
    @property
    def name(self) -> str:
        """Legacy display-name accessor until runtime consumers move to slug."""
        return self.title

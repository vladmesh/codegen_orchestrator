from enum import StrEnum
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, Field

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
    """Create project request. The API validates incoming bodies against this.

    Fields are the columns of `shared.models.project.Project` a caller may set.
    Module choice and free-text description live inside `config`, where the
    scaffolder and the developer node read them from.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    title: str
    status: ProjectStatus = ProjectStatus.DRAFT
    config: dict[str, Any] = {}
    # The run this project's work is being done for. Required: this is where a
    # run identity enters the system, and a project created without one would
    # be a project whose workers cannot be attributed to anything once they are
    # dead. The caller that starts the run supplies its own id here — the live
    # harness its manifest run id, the PO agent the request it opened.
    initiating_run_id: str = Field(min_length=1, max_length=64)


class ProjectUpdate(BaseModel):
    """Update project request, for both PUT and PATCH."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    status: ProjectStatus | None = None
    config: dict[str, Any] | None = None
    project_spec: dict | None = None


class TeardownStatus(StrEnum):
    """How far a project teardown has got.

    The undeploy runs over SSH on another service, so requesting a teardown and
    finishing one are two different moments. Only `completed` means the containers
    are down: until then the bot is still polling on its token and rebinding it
    elsewhere would lose the race with Telegram.
    """

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class ProjectTeardownResult(BaseModel):
    """Where a project teardown stands.

    `pending_application_ids` are the applications not yet confirmed down. While
    that list is non-empty the project keeps its bot on purpose — the binding is
    released only once the undeploy reports back, so `completed` is the one status
    that says the token is reusable.
    """

    project_id: uuid.UUID
    status: TeardownStatus
    project_status: ProjectStatus
    pending_application_ids: list[int] = []
    released_bot_username: str | None = None
    error: str | None = None


class ProjectDTO(TimestampedDTO):
    """Project response."""

    id: uuid.UUID
    title: str
    slug: str
    description: str | None = None
    status: ProjectStatus
    modules: list[ServiceModule] = []
    config: dict = {}
    owner_id: int
    project_spec: dict | None = None
    # The run that initiated this project's work. Consumers read it from here to
    # stamp ownership on the workers they create.
    initiating_run_id: str

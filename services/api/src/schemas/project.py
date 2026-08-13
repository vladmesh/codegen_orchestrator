"""Project schemas."""

from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, model_validator

from shared.contracts.bot_access import parse_allowed_telegram_ids
from shared.contracts.dto.base import TimestampedDTO

# The request schemas are the contract every client already imports; the API
# validates against that same object rather than a look-alike of its own.
from shared.contracts.dto.project import ProjectCreate, ProjectStatus, ProjectUpdate

__all__ = [
    "BotAccessRequest",
    "MergeSecretsRequest",
    "ProjectBase",
    "ProjectCreate",
    "ProjectRead",
    "ProjectUpdate",
]


class ProjectBase(BaseModel):
    """Base project schema."""

    id: uuid.UUID
    title: str
    slug: str
    status: str = ProjectStatus.DRAFT.value
    config: dict[str, Any] = {}


class ProjectRead(ProjectBase, TimestampedDTO):
    """Schema for reading a project."""

    model_config = ConfigDict(from_attributes=True)

    owner_id: int
    project_spec: dict | None = None
    # The run this project's work belongs to. Every consumer that creates a
    # worker reads it from here, so it has to leave the API with the project.
    initiating_run_id: str


class MergeSecretsRequest(BaseModel):
    """Schema for atomic secret merge."""

    secrets: dict[str, str]
    env_hints: dict[str, str] | None = None


class BotAccessRequest(BaseModel):
    """The product audience selected for a Telegram bot."""

    model_config = ConfigDict(extra="forbid")

    mode: str
    allowed_telegram_ids: str = ""

    @model_validator(mode="after")
    def _private_audience_is_not_empty(self) -> "BotAccessRequest":
        if self.mode not in {"only_me", "public", "custom"}:
            raise ValueError("mode must be only_me, public, or custom")
        if self.mode != "public" and not parse_allowed_telegram_ids(self.allowed_telegram_ids):
            raise ValueError("a private bot audience must contain a Telegram ID")
        if self.mode == "public" and self.allowed_telegram_ids != "":
            raise ValueError("a public bot audience must be empty")
        return self

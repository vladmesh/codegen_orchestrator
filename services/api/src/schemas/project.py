"""Project schemas."""

from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, model_validator

from shared.contracts.dto.base import TimestampedDTO
from shared.contracts.dto.project import ProjectStatus, ServiceModule


class ProjectBase(BaseModel):
    """Base project schema."""

    id: uuid.UUID
    title: str
    slug: str
    status: str = ProjectStatus.DRAFT.value
    config: dict[str, Any] = {}


class ProjectCreate(BaseModel):
    """Schema for creating a project."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None  # Auto-generated if not provided
    title: str
    status: str = ProjectStatus.DRAFT.value
    config: dict[str, Any] = {}
    modules: list[ServiceModule] = [ServiceModule.BACKEND]


class ProjectRead(ProjectBase, TimestampedDTO):
    """Schema for reading a project."""

    model_config = ConfigDict(from_attributes=True)

    owner_id: int


class ProjectUpdate(BaseModel):
    """Schema for updating a project."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    status: str | None = None
    config: dict[str, Any] | None = None


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
        if self.mode not in {"only_me", "public", "invite", "custom"}:
            raise ValueError("mode must be only_me, public, invite, or custom")
        if self.mode != "public" and not self.allowed_telegram_ids.strip():
            raise ValueError("a private bot audience must not be empty")
        if self.mode == "public" and self.allowed_telegram_ids:
            raise ValueError("a public bot audience must be empty")
        return self

"""Project schemas."""

from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.contracts.bot_access import parse_allowed_telegram_ids
from shared.contracts.dto.base import TimestampedDTO

# The request schemas are the contract every client already imports; the API
# validates against that same object rather than a look-alike of its own.
from shared.contracts.dto.project import ProjectCreate, ProjectStatus, ProjectUpdate

__all__ = [
    "BotAccessRequest",
    "BotUserMutationRequest",
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
    # `None` only for rows that predate run ownership; those cannot create
    # workers at all — see `require_initiating_run`.
    initiating_run_id: str | None = None


class MergeSecretsRequest(BaseModel):
    """Schema for atomic secret merge."""

    secrets: dict[str, str]
    env_hints: dict[str, str] | None = None


class BotAccessRequest(BaseModel):
    """The product audience selected for a Telegram bot."""

    model_config = ConfigDict(extra="forbid")

    mode: str
    allowed_telegram_ids: str = ""
    # A private bot normally includes the project owner. This escape hatch is
    # deliberately accepted only from an internal service acting for itself;
    # the route enforces that authorization because the schema has no caller.
    allow_ownerless_audience: bool = False

    @model_validator(mode="after")
    def _private_audience_is_not_empty(self) -> "BotAccessRequest":
        if self.mode not in {"only_me", "public", "custom"}:
            raise ValueError("mode must be only_me, public, or custom")
        if self.mode != "public" and not parse_allowed_telegram_ids(self.allowed_telegram_ids):
            raise ValueError("a private bot audience must contain a Telegram ID")
        if self.mode == "public" and self.allowed_telegram_ids != "":
            raise ValueError("a public bot audience must be empty")
        if self.mode == "public" and self.allow_ownerless_audience:
            raise ValueError("a public bot does not need allow_ownerless_audience")
        return self


# Telegram IDs are positive integers well above any port number; a 0 or negative
# value is never a Telegram account id.
MIN_TELEGRAM_ID = 1


class BotUserMutationRequest(BaseModel):
    """One typed Telegram ID to add to (or remove from) the chosen audience.

    The body carries exactly one ID on purpose: the conversational operation is
    "add user X", and a caller that wanted to replace the whole list would have
    to use set_bot_access instead of smuggling a replacement through here.
    """

    model_config = ConfigDict(extra="forbid")

    telegram_id: int = Field(strict=True, ge=MIN_TELEGRAM_ID)

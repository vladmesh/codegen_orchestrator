"""Project schemas."""

from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, Field

from shared.contracts.dto.base import TimestampedDTO

# The request schemas are the contract every client already imports; the API
# validates against that same object rather than a look-alike of its own.
from shared.contracts.dto.project import ProjectCreate, ProjectStatus, ProjectUpdate

__all__ = [
    "GrantUserRequest",
    "MergeSecretsRequest",
    "OwnershipTransferRequest",
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


class GrantUserRequest(BaseModel):
    """One verified Telegram identity to give permanent access."""

    model_config = ConfigDict(extra="forbid")
    telegram_id: int = Field(strict=True, ge=1)


class OwnershipTransferRequest(GrantUserRequest):
    """The verified incoming owner; transfer waits for active readback."""


class MergeSecretsRequest(BaseModel):
    """Schema for atomic secret merge."""

    secrets: dict[str, str]
    env_hints: dict[str, str] | None = None

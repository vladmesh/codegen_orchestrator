"""Repository API schemas."""

import uuid

from pydantic import ConfigDict

from shared.contracts.dto.base import TimestampedDTO

# The request schemas are the contract every client already imports; the API
# validates against that same object rather than a look-alike of its own.
from shared.contracts.dto.repository import RepositoryCreate, RepositoryUpdate

__all__ = [
    "RepositoryCreate",
    "RepositoryRead",
    "RepositoryUpdate",
]


class RepositoryRead(TimestampedDTO):
    """Schema for reading a repository."""

    id: str
    project_id: uuid.UUID
    name: str
    git_url: str
    provider_repo_id: int | None
    role: str
    visibility: str
    is_managed: bool
    acceptance_criteria: str | None = None
    bot_username: str | None = None

    model_config = ConfigDict(from_attributes=True)

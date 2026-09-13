"""Request schemas for admin action endpoints."""

import uuid

from pydantic import BaseModel, Field


class AdminAction(BaseModel):
    """Minimal body for admin-triggered actions."""

    actor: str = "admin"


class E2ERunAction(AdminAction):
    """Story owner required by an administrative QA executor run."""

    story_id: str = Field(min_length=1)


class SpawnWorkerRequest(BaseModel):
    """Request body for POST /tasks/{id}/spawn-worker."""

    actor: str = "admin"
    description: str | None = None


class FromRepoRequest(BaseModel):
    """Request body for POST /applications/from-repo."""

    repo_url: str
    project_id: uuid.UUID
    server_handle: str
    service_name: str
    actor: str = "admin"

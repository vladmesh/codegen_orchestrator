"""Brainstorm API schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class BrainstormCreate(BaseModel):
    """Schema for creating a brainstorm."""

    project_id: str
    title: str
    content: str | None = None
    created_by: str = "system"


class BrainstormRead(BaseModel):
    """Schema for reading a brainstorm."""

    id: str
    project_id: str
    title: str
    content: str | None
    status: str
    created_by: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BrainstormUpdate(BaseModel):
    """Schema for updating a brainstorm (non-status fields only)."""

    title: str | None = None
    content: str | None = None


class BrainstormTransition(BaseModel):
    """Schema for action endpoints (done, triage, archive)."""

    reason: str | None = None
    actor: str = "system"

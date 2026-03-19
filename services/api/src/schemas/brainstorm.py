"""Brainstorm API schemas."""

import uuid

from pydantic import BaseModel, ConfigDict

from shared.contracts.dto.base import TimestampedDTO


class BrainstormCreate(BaseModel):
    """Schema for creating a brainstorm."""

    project_id: uuid.UUID
    title: str
    content: str | None = None
    created_by: str = "system"


class BrainstormRead(TimestampedDTO):
    """Schema for reading a brainstorm."""

    id: str
    project_id: uuid.UUID
    title: str
    content: str | None
    status: str
    created_by: str

    model_config = ConfigDict(from_attributes=True)


class BrainstormUpdate(BaseModel):
    """Schema for updating a brainstorm (non-status fields only)."""

    title: str | None = None
    content: str | None = None


class BrainstormTransition(BaseModel):
    """Schema for action endpoints (done, triage, archive)."""

    reason: str | None = None
    actor: str = "system"

"""Milestone API schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MilestoneCreate(BaseModel):
    """Schema for creating a milestone."""

    project_id: str
    title: str
    description: str | None = None
    sort_order: int = 0
    parent_id: str | None = None
    created_by: str = "system"


class MilestoneRead(BaseModel):
    """Schema for reading a milestone."""

    id: str
    project_id: str
    title: str
    description: str | None
    sort_order: int
    status: str
    parent_id: str | None
    created_by: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MilestoneUpdate(BaseModel):
    """Schema for updating a milestone (non-status fields only)."""

    title: str | None = None
    description: str | None = None
    sort_order: int | None = None
    parent_id: str | None = None


class MilestoneTransition(BaseModel):
    """Schema for action endpoints (complete)."""

    reason: str | None = None
    actor: str = "system"

"""Story API schemas."""

from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict

from shared.contracts.dto.base import TimestampedDTO

# The request schemas are the contract every client already imports; the API
# validates against those same objects rather than look-alikes of its own.
from shared.contracts.dto.story import StoryCreate, StoryUpdate

__all__ = [
    "StoryCreate",
    "StoryRead",
    "StoryReopen",
    "StoryTransition",
    "StoryUpdate",
]


class StoryRead(TimestampedDTO):
    """Schema for reading a story."""

    id: str
    project_id: uuid.UUID
    parent_story_id: str | None
    title: str
    description: str | None
    acceptance_criteria: str | None
    type: str
    status: str
    priority: int
    blocked_by_story_id: str | None
    created_by: str
    user_report: str | None
    quarantine_reason: dict[str, Any] | None = None
    pr_number: int | None = None

    model_config = ConfigDict(from_attributes=True)


class StoryReopen(BaseModel):
    """Schema for reopening a completed story with user feedback."""

    user_report: str | None = None
    actor: str = "system"


class StoryTransition(BaseModel):
    """Schema for story status transition actions."""

    actor: str = "system"

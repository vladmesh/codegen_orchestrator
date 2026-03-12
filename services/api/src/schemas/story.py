"""Story API schemas."""

from datetime import datetime
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict

from shared.contracts.dto.story import StoryType


class StoryCreate(BaseModel):
    """Schema for creating a story."""

    project_id: uuid.UUID
    title: str
    description: str | None = None
    acceptance_criteria: str | None = None
    parent_story_id: str | None = None
    type: Literal[StoryType.PRODUCT, StoryType.TECHNICAL] = StoryType.PRODUCT
    priority: int = 0
    blocked_by_story_id: str | None = None
    created_by: str = "system"


class StoryRead(BaseModel):
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
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class StoryUpdate(BaseModel):
    """Schema for updating a story."""

    title: str | None = None
    description: str | None = None
    acceptance_criteria: str | None = None
    parent_story_id: str | None = None
    type: Literal[StoryType.PRODUCT, StoryType.TECHNICAL] | None = None
    priority: int | None = None
    blocked_by_story_id: str | None = None


class StoryReopen(BaseModel):
    """Schema for reopening a completed story with user feedback."""

    user_report: str | None = None
    actor: str = "system"


class StoryTransition(BaseModel):
    """Schema for story status transition actions."""

    actor: str = "system"

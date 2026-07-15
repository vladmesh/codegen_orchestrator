"""Story DTOs and enums — single source of truth for story statuses and transitions."""

from enum import StrEnum
import uuid

from pydantic import BaseModel

from shared.contracts.dto.base import TimestampedDTO


class StoryType(StrEnum):
    PRODUCT = "product"
    TECHNICAL = "technical"


class StoryStatus(StrEnum):
    CREATED = "created"
    IN_PROGRESS = "in_progress"
    REOPENED = "reopened"
    PR_REVIEW = "pr_review"
    DEPLOYING = "deploying"
    TESTING = "testing"
    WAITING_HUMAN_REVIEW = "waiting_human_review"
    COMPLETED = "completed"
    FAILED = "failed"
    ARCHIVED = "archived"


VALID_TRANSITIONS: dict[StoryStatus, set[StoryStatus]] = {
    StoryStatus.CREATED: {StoryStatus.IN_PROGRESS, StoryStatus.FAILED, StoryStatus.ARCHIVED},
    StoryStatus.IN_PROGRESS: {
        StoryStatus.PR_REVIEW,
        StoryStatus.DEPLOYING,
        StoryStatus.WAITING_HUMAN_REVIEW,
        StoryStatus.COMPLETED,
        StoryStatus.FAILED,
        StoryStatus.ARCHIVED,
    },
    StoryStatus.REOPENED: {
        StoryStatus.IN_PROGRESS,
        StoryStatus.FAILED,
    },
    StoryStatus.PR_REVIEW: {
        StoryStatus.DEPLOYING,
        StoryStatus.IN_PROGRESS,
        StoryStatus.WAITING_HUMAN_REVIEW,
        StoryStatus.FAILED,
    },
    StoryStatus.DEPLOYING: {
        StoryStatus.TESTING,
        StoryStatus.COMPLETED,
        StoryStatus.IN_PROGRESS,
        StoryStatus.FAILED,
    },
    StoryStatus.TESTING: {
        StoryStatus.COMPLETED,
        StoryStatus.IN_PROGRESS,
        StoryStatus.FAILED,
    },
    StoryStatus.WAITING_HUMAN_REVIEW: {
        StoryStatus.IN_PROGRESS,
        StoryStatus.FAILED,
    },
    StoryStatus.COMPLETED: {StoryStatus.REOPENED, StoryStatus.ARCHIVED},
    StoryStatus.FAILED: {StoryStatus.REOPENED},
    StoryStatus.ARCHIVED: set(),
}


# --- Response DTOs ---


class StoryDTO(TimestampedDTO):
    """Story response from API."""

    id: str
    project_id: uuid.UUID
    parent_story_id: str | None = None
    title: str
    description: str | None = None
    acceptance_criteria: str | None = None
    type: StoryType
    status: StoryStatus
    priority: int
    blocked_by_story_id: str | None = None
    created_by: str
    user_report: str | None = None
    pr_number: int | None = None


# --- Request DTOs ---


class StoryCreate(BaseModel):
    """Create story request."""

    project_id: uuid.UUID
    title: str
    description: str | None = None
    acceptance_criteria: str | None = None
    parent_story_id: str | None = None
    type: StoryType = StoryType.PRODUCT
    priority: int = 0
    blocked_by_story_id: str | None = None
    created_by: str = "system"


class StoryUpdate(BaseModel):
    """Update story request."""

    title: str | None = None
    description: str | None = None
    acceptance_criteria: str | None = None
    parent_story_id: str | None = None
    type: StoryType | None = None
    priority: int | None = None
    blocked_by_story_id: str | None = None

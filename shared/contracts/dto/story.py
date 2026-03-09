"""Story DTOs and enums — single source of truth for story statuses and transitions."""

from enum import StrEnum


class StoryType(StrEnum):
    PRODUCT = "product"
    TECHNICAL = "technical"


class StoryStatus(StrEnum):
    CREATED = "created"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    ARCHIVED = "archived"


VALID_TRANSITIONS: dict[StoryStatus, set[StoryStatus]] = {
    StoryStatus.CREATED: {StoryStatus.IN_PROGRESS, StoryStatus.FAILED, StoryStatus.ARCHIVED},
    StoryStatus.IN_PROGRESS: {
        StoryStatus.COMPLETED,
        StoryStatus.FAILED,
        StoryStatus.ARCHIVED,
    },
    StoryStatus.COMPLETED: {StoryStatus.IN_PROGRESS, StoryStatus.ARCHIVED},
    StoryStatus.FAILED: set(),
    StoryStatus.ARCHIVED: set(),
}

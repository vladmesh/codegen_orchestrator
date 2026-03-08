"""Story DTOs and enums — single source of truth for story statuses and transitions."""

from enum import StrEnum


class StoryStatus(StrEnum):
    CREATED = "created"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ARCHIVED = "archived"


VALID_TRANSITIONS: dict[StoryStatus, set[StoryStatus]] = {
    StoryStatus.CREATED: {StoryStatus.IN_PROGRESS, StoryStatus.ARCHIVED},
    StoryStatus.IN_PROGRESS: {StoryStatus.COMPLETED, StoryStatus.ARCHIVED},
    StoryStatus.COMPLETED: {StoryStatus.IN_PROGRESS, StoryStatus.ARCHIVED},
    StoryStatus.ARCHIVED: set(),
}

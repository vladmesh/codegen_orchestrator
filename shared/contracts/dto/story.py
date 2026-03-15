"""Story DTOs and enums — single source of truth for story statuses and transitions."""

from enum import StrEnum


class StoryType(StrEnum):
    PRODUCT = "product"
    TECHNICAL = "technical"


class StoryStatus(StrEnum):
    CREATED = "created"
    IN_PROGRESS = "in_progress"
    REOPENED = "reopened"
    PR_REVIEW = "pr_review"
    DEPLOYING = "deploying"
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
        StoryStatus.FAILED,
    },
    StoryStatus.DEPLOYING: {
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

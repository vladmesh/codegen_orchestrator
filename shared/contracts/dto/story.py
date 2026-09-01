"""Story DTOs and enums — single source of truth for story statuses and transitions."""

from datetime import datetime
from enum import StrEnum
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    WAITING_USER_SECRET = "waiting_user_secret"  # noqa: S105
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
        StoryStatus.WAITING_USER_SECRET,
        # A deploy can be refused by infrastructure in a way no wait resolves —
        # a request no managed server fits, or a fleet the platform cannot see.
        # The story is not defective, so it must not be failed; it belongs in
        # the human-review queue, which it could not reach from here before.
        StoryStatus.WAITING_HUMAN_REVIEW,
        StoryStatus.FAILED,
    },
    StoryStatus.TESTING: {
        StoryStatus.COMPLETED,
        StoryStatus.IN_PROGRESS,
        StoryStatus.WAITING_HUMAN_REVIEW,
        StoryStatus.FAILED,
    },
    StoryStatus.WAITING_HUMAN_REVIEW: {
        StoryStatus.IN_PROGRESS,
        StoryStatus.DEPLOYING,
        StoryStatus.COMPLETED,
        StoryStatus.FAILED,
    },
    StoryStatus.WAITING_USER_SECRET: {
        StoryStatus.DEPLOYING,
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
    quarantine_reason: dict[str, Any] | None = None
    operator_acceptance: "StoryAcceptance | None" = None
    operator_recheck: "StoryRecheck | None" = None
    reopened_at: datetime | None = None
    pr_number: int | None = None
    product_brief_id: str | None = None


# --- Request DTOs ---


class StoryCreate(BaseModel):
    """Create story request."""

    project_id: uuid.UUID
    title: str
    description: str | None = None
    acceptance_criteria: str | None = None
    parent_story_id: str | None = None
    type: StoryType
    priority: int = 0
    blocked_by_story_id: str | None = None
    created_by: str = "system"
    product_brief_id: str | None = None


class StoryUpdate(BaseModel):
    """Update story request."""

    title: str | None = None
    description: str | None = None
    acceptance_criteria: str | None = None
    parent_story_id: str | None = None
    type: StoryType | None = None
    priority: int | None = None
    blocked_by_story_id: str | None = None
    quarantine_reason: dict[str, Any] | None = None
    pr_number: int | None = None


class StoryAcceptance(BaseModel):
    """The durable human decision that completed a reviewed story."""

    model_config = ConfigDict(extra="forbid")

    actor: str
    basis: str
    accepted_at: datetime
    overridden_quarantine_reason: dict[str, Any] | None = None


class StoryAccept(BaseModel):
    """The operator-supplied grounds for accepting a reviewed result."""

    model_config = ConfigDict(extra="forbid")

    basis: str = Field(min_length=1)

    @field_validator("basis")
    @classmethod
    def _basis_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("basis must not be blank")
        return value


class StoryRecheckMode(StrEnum):
    """The ordinary pipeline stage an operator recheck entered."""

    DEPLOY = "deploy"


class StoryRecheck(BaseModel):
    """Durable operator decision to re-verify a quarantined QA target."""

    model_config = ConfigDict(extra="forbid")

    id: str
    actor: str
    basis: str
    rechecked_at: datetime
    mode: StoryRecheckMode
    application_id: int
    run_id: str
    rechecked_quarantine_reason: dict[str, Any]

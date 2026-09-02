"""Story DTOs and enums — single source of truth for story statuses and transitions."""

from datetime import datetime
from enum import StrEnum
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class StoryWaitingOn(StrEnum):
    """What a Story is waiting for — the typed answer to "why is this parked?".

    The value is not a status of its own and it is not a transition: it is what
    the status a transition lands on implies, written by the same server action
    that performs the transition. ``NONE`` means the story is not waiting on
    anything outside itself, which includes both live work and every ending.
    """

    NONE = "none"
    CI = "ci"
    DEPLOY = "deploy"
    QA = "qa"
    USER_SECRET = "user_secret"  # noqa: S105
    HUMAN_REVIEW = "human_review"
    RESOURCES = "resources"


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

#: The one declared mapping from the status a transition lands on to the
#: ``waiting_on`` that status implies.  Adding it is not a change to
#: ``VALID_TRANSITIONS``: no edge moves, and no status is added or removed.
#:
#: It is total over ``StoryStatus`` on purpose — every landing has an answer, so
#: a transition can never leave a stale wait behind.  ``RESOURCES`` is declared
#: and unmapped: work parks for resources at the *Task* level while the Story
#: stays ``IN_PROGRESS``, so no Story status implies it.
WAITING_ON_BY_STATUS: dict[StoryStatus, StoryWaitingOn] = {
    StoryStatus.CREATED: StoryWaitingOn.NONE,
    StoryStatus.IN_PROGRESS: StoryWaitingOn.NONE,
    StoryStatus.REOPENED: StoryWaitingOn.NONE,
    StoryStatus.PR_REVIEW: StoryWaitingOn.CI,
    StoryStatus.DEPLOYING: StoryWaitingOn.DEPLOY,
    StoryStatus.TESTING: StoryWaitingOn.QA,
    StoryStatus.WAITING_HUMAN_REVIEW: StoryWaitingOn.HUMAN_REVIEW,
    StoryStatus.WAITING_USER_SECRET: StoryWaitingOn.USER_SECRET,
    StoryStatus.COMPLETED: StoryWaitingOn.NONE,
    StoryStatus.FAILED: StoryWaitingOn.NONE,
    StoryStatus.ARCHIVED: StoryWaitingOn.NONE,
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
    # Paired with ``StoryRead.waiting_on``: what the story is waiting for, written
    # by the transition that landed it on ``status``.  Required, with no default:
    # the column is non-nullable, migration ``c3f7a91d2b48`` backfilled every
    # existing row, and ``StoryRead`` always returns it — so a response without
    # the field is a broken response, not a story waiting for nothing, and it
    # must raise here rather than be interpreted into an invented ``none``.
    waiting_on: StoryWaitingOn
    priority: int
    blocked_by_story_id: str | None = None
    created_by: str
    user_report: str | None = None
    quarantine_reason: dict[str, Any] | None = None
    operator_acceptance: "StoryAcceptance | None" = None
    operator_recheck: "StoryRecheck | None" = None
    reopened_at: datetime | None = None
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


#: Story fields a transition owns.  Sending one to ``PATCH /stories/{id}`` is a
#: caller bug, not a no-op, so it is refused instead of dropped by
#: ``extra="ignore"``.
TRANSITION_OWNED_STORY_FIELDS: tuple[str, ...] = ("status", "waiting_on")


class StoryUpdate(BaseModel):
    """Update story request — the editorial fields, never the lifecycle ones.

    ``status`` was never patchable; ``waiting_on`` is refused on the same
    grounds and out loud.  Both are written only by the server actions that
    perform a transition, so a poller that thinks it knows what a story waits
    for gets a 422 rather than a field it silently clobbered.
    """

    title: str | None = None
    description: str | None = None
    acceptance_criteria: str | None = None
    parent_story_id: str | None = None
    type: StoryType | None = None
    priority: int | None = None
    blocked_by_story_id: str | None = None
    quarantine_reason: dict[str, Any] | None = None
    pr_number: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _refuse_transition_owned_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            offending = [name for name in TRANSITION_OWNED_STORY_FIELDS if name in data]
            if offending:
                raise ValueError(
                    f"{', '.join(offending)} is written by Story transitions only"
                    " and cannot be patched"
                )
        return data


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

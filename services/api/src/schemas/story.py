"""Story API schemas."""

from datetime import datetime
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, Field

from shared.contracts.dto.base import TimestampedDTO
from shared.contracts.dto.owner_notification import OwnerNotification

# The request schemas are the contract every client already imports; the API
# validates against those same objects rather than look-alikes of its own.
from shared.contracts.dto.story import (
    StoryAccept,
    StoryAcceptance,
    StoryCreate,
    StoryRecheck,
    StoryStatus,
    StoryType,
    StoryUnverifiedDecision,
    StoryUnverifiedDecisionCreate,
    StoryUpdate,
    StoryWaitingOn,
)
from shared.contracts.dto.story_failure import StoryFailure
from shared.contracts.dto.story_planning import StoryPlanning

__all__ = [
    "StoryCreate",
    "StoryAccept",
    "StoryAcceptance",
    "StoryRecheck",
    "StoryRead",
    "StoryOwnerNotificationRead",
    "StoryReopen",
    "StoryStatus",
    "StoryStopTransition",
    "StoryTransition",
    "StoryType",
    "StoryUnverifiedDecision",
    "StoryUnverifiedDecisionCreate",
    "StoryUpdate",
    "StoryWaitingOn",
]


class StoryRead(TimestampedDTO):
    """Schema for reading a story.

    One half of the Story response contract; the shared `StoryDTO` every client
    parses with is the other.  `services/api/tests/unit/test_story_schemas.py`
    holds the two to the same field spec — name, annotation, requiredness and
    default — so a field cannot go missing, change type or become optional on
    one side alone.  Keep any change here paired with `StoryDTO`.
    """

    id: str
    project_id: uuid.UUID
    parent_story_id: str | None = None
    title: str
    description: str | None = None
    acceptance_criteria: str | None = None
    type: StoryType
    status: StoryStatus
    # Typed, and written by the transition that landed the story on `status` —
    # never by a reader of this schema.
    waiting_on: StoryWaitingOn
    priority: int
    blocked_by_story_id: str | None = None
    created_by: str
    user_report: str | None = None
    quarantine_reason: dict[str, Any] | None = None
    generated_product_timeline: dict[str, Any] | None = None
    operator_acceptance: StoryAcceptance | None = None
    operator_recheck: StoryRecheck | None = None
    unverified_decisions: list[StoryUnverifiedDecision] = Field(default_factory=list)
    reopened_at: datetime | None = None
    pr_number: int | None = None
    # How the last planning attempt ended: channels that planned it, or the
    # failure and whether the platform is still retrying.
    planning: StoryPlanning | None = None

    model_config = ConfigDict(from_attributes=True)


class StoryReopen(BaseModel):
    """Schema for reopening a completed story with user feedback."""

    user_report: str | None = None
    actor: str = "system"


class StoryTransition(BaseModel):
    """Schema for story status transition actions."""

    actor: str = "system"
    # The terminal QA run whose verdict this transition routes. The API stamps
    # that run as routed in the transition's own transaction.
    qa_run_id: str | None = None


class StoryStopTransition(StoryTransition):
    """The body of the two stopping actions, `fail` and `human-review`.

    ``failure`` names the platform failure that stopped the story. The API
    stores it as the story's ``quarantine_reason`` and owes the owner and
    administrators the matching notice, in the transition's own transaction.
    """

    failure: StoryFailure | None = None


class StoryOwnerNotificationRead(BaseModel):
    """Internal recovery view of a story-backed owner notification."""

    id: str
    owner_notification: OwnerNotification

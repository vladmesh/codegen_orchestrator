"""The typed vocabulary of the Product Brief coverage-to-dispatch boundary.

Four questions live here, and each has exactly one answer type:

* what the user confirmed — `ProductBriefContent`, frozen at confirmation;
* who owns the incomplete plan — `ProductBriefPlanningAttemptRead`;
* how one must-requirement was disposed of — `RequirementCoverageRead`;
* whether the plan may be released — `ProductBriefAdmissionRead`.

The admission answer is a typed outcome rather than an HTTP status, because
calling admit twice is not an error: the second call reports
`ALREADY_ADMITTED` and releases nothing, and an incomplete brief reports which
requirement ids are still undisposed instead of failing.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: How long an architect's claim survives without a heartbeat. A claim whose
#: heartbeat is older than this is stale and may be taken over; a fresher one
#: makes a second claim report `IN_PROGRESS` instead of issuing a rival attempt.
PLANNING_ATTEMPT_HEARTBEAT_TIMEOUT_SECONDS = 90


class MustRequirement(BaseModel):
    """One thing the product must do, addressed by id in every disposition."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=10000)

    @field_validator("id", "text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("requirement fields must not be blank")
        return value


class ProductBriefContent(BaseModel):
    """The confirmed brief document. Frozen once `confirmed_at` is stamped."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=10000)
    must_requirements: list[MustRequirement] = Field(min_length=1)

    @model_validator(mode="after")
    def _requirement_ids_are_unique(self) -> ProductBriefContent:
        ids = [requirement.id for requirement in self.must_requirements]
        if len(ids) != len(set(ids)):
            raise ValueError("must requirement ids must be unique")
        return self


class ProductBriefCreate(BaseModel):
    """Open a new revision of a project's brief. Never edits an existing one."""

    model_config = ConfigDict(extra="forbid")

    project_id: uuid.UUID
    title: str = Field(min_length=1, max_length=500)
    content: ProductBriefContent
    #: Idempotency key. A retry of the same creation returns the revision it
    #: already opened rather than opening a second one.
    request_id: str = Field(min_length=1, max_length=255)


class ProductBriefConfirm(BaseModel):
    """Freeze the presented revision. The content is echoed back, not replaced.

    The caller sends the content it showed the user, and confirmation refuses
    unless it is byte-for-byte the stored revision. A user who confirms
    something other than what is stored is confirming a different brief, and a
    different brief is a new revision.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=255)
    content: ProductBriefContent


class ProductBriefStoryBind(BaseModel):
    """Bind a confirmed brief to the story its plan will be built in."""

    model_config = ConfigDict(extra="forbid")

    story_id: str = Field(min_length=1, max_length=255)


class ProductBriefRead(BaseModel):
    """One brief revision, including the whole of its planning-attempt state."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: str
    project_id: uuid.UUID
    story_id: str | None = None
    revision: int
    title: str
    content: ProductBriefContent
    confirmed_at: datetime | None = None
    confirmation_request_id: str | None = None
    coverage_admitted_at: datetime | None = None
    planning_attempt_id: str | None = None
    planning_attempt_active: bool
    planning_attempt_heartbeat_at: datetime | None = None


class ProductBriefPlanningAttemptOutcome(StrEnum):
    """What a claim, heartbeat or finish did to the ownership of the plan."""

    #: This caller now owns the incomplete plan, and the attempt id says which
    #: attempt it owns. A takeover of a stale attempt says this too — with a new
    #: attempt id, which is what strands the previous owner.
    CLAIMED = "claimed"
    #: Another architect owns it and its heartbeat is fresh. Nothing was issued.
    IN_PROGRESS = "in_progress"
    #: The brief's coverage is already admitted, so there is no incomplete plan
    #: to own. Nothing was issued.
    ALREADY_ADMITTED = "already_admitted"
    #: The attempt this caller presented is over — it finished it, or it had
    #: already been taken over.
    RELEASED = "released"


class ProductBriefPlanningAttemptRead(BaseModel):
    """Who owns the incomplete plan of this brief, after this call."""

    model_config = ConfigDict(extra="forbid")

    brief_id: str
    story_id: str
    outcome: ProductBriefPlanningAttemptOutcome
    #: The attempt id that owns the plan now. The caller may act as the planner
    #: only when this is the id it holds — an `IN_PROGRESS` answer names the
    #: rival attempt, not the caller's.
    planning_attempt_id: str | None = None
    planning_attempt_heartbeat_at: datetime | None = None


class ProductBriefPlanningAttemptCommand(BaseModel):
    """The attempt a heartbeat, a coverage write or an admission acts under."""

    model_config = ConfigDict(extra="forbid")

    planning_attempt_id: str = Field(min_length=1, max_length=128)


class RequirementCoverageCreate(BaseModel):
    """How the architect disposed of one must-requirement.

    Exactly one disposition: a task that covers it, or a reason it was returned.
    Neither is not a disposition, and both at once is two answers to one
    question.
    """

    model_config = ConfigDict(extra="forbid")

    requirement_id: str = Field(min_length=1, max_length=128)
    planning_attempt_id: str = Field(min_length=1, max_length=128)
    task_id: str | None = Field(default=None, max_length=255)
    returned_reason: str | None = Field(default=None, max_length=10000)

    @model_validator(mode="after")
    def _exactly_one_disposition(self) -> RequirementCoverageCreate:
        if bool(self.task_id) == bool(self.returned_reason):
            raise ValueError("coverage needs either a task or a returned reason, not both")
        return self


class RequirementCoverageRead(BaseModel):
    """One recorded disposition."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: int
    brief_id: str
    requirement_id: str
    planning_attempt_id: str
    task_id: str | None = None
    returned_reason: str | None = None


class ProductBriefAdmissionOutcome(StrEnum):
    """The three answers the one admission step can give."""

    #: Every must-requirement was disposed of, `coverage_admitted_at` is stamped
    #: and `released_task_ids` names the tasks this call released.
    ADMITTED = "admitted"
    #: The boundary was already crossed. Nothing was released a second time.
    ALREADY_ADMITTED = "already_admitted"
    #: `missing_requirement_ids` names what is still undisposed. Nothing moved.
    INCOMPLETE = "incomplete"


class ProductBriefAdmissionRead(BaseModel):
    """The durable result of the one coverage-to-dispatch admission step."""

    model_config = ConfigDict(extra="forbid")

    brief_id: str
    story_id: str
    outcome: ProductBriefAdmissionOutcome
    coverage_admitted_at: datetime | None = None
    missing_requirement_ids: list[str] = Field(default_factory=list)
    #: The tasks this call moved from unadmitted to dispatchable. Empty on every
    #: outcome but the first `ADMITTED` one, which is what "releases nothing
    #: twice" means when read off the response.
    released_task_ids: list[str] = Field(default_factory=list)

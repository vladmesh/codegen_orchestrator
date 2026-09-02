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

**Two shapes of the same document.** `ProductBriefContent` is the *read* shape:
it is what `ProductBriefRead` parses out of the JSON column, so it must keep
parsing every document the released API has already stored.
`ProposedProductBriefContent` is the *write* shape, and it is what
`ProductBriefCreate` and `ProductBriefConfirm` carry: a producer may not open a
revision whose must-requirement id is not path-safe, or whose requirement
carries neither the user's wording nor a reference to it. The strictness sits on
the write boundary rather than on the field defaults precisely so that adding it
is additive — nothing stored becomes unreadable, and nothing new can be written
without it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import re
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: How long an architect's claim survives without a heartbeat. A claim whose
#: heartbeat is older than this is stale and may be taken over; a fresher one
#: makes a second claim report `IN_PROGRESS` instead of issuing a rival attempt.
PLANNING_ATTEMPT_HEARTBEAT_TIMEOUT_SECONDS = 90


#: A must-requirement id is addressed as one path segment —
#: `PUT /product-briefs/{id}/coverage/{requirement_id}` — so an id that carries
#: `/`, `%`, `?`, whitespace or a leading dot does not name the requirement it
#: was meant to name. It resolves to another route, or to none at all, and the
#: architect's disposition comes back as a 404 that says nothing about why. The
#: refusal therefore belongs where the revision is opened.
REQUIREMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: The shape of a Telegram bot token — the credential a PO most often holds for
#: a project. `services/api/src/utils/telegram_token.py` owns the authoritative
#: copy for *validating* a token; this one answers a different question at a
#: different boundary: whether credential material is being written into a brief
#: that the architect, and therefore an LLM, will read back.
_CREDENTIAL_VALUE_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")

#: Name fragments that mean "this is a credential, not a product setting". A
#: setting is a value the user may change; a secret is resolved by Python at the
#: execution boundary and never travels through a document an LLM reads.
_CREDENTIAL_KEY_FRAGMENTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "api_key",
    "apikey",
    "private_key",
)

#: The shape of a manifest-declared settings key: one path segment per level,
#: as a generated product's `settings_schema` names its properties.
SETTING_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")


class SettingScope(StrEnum):
    """The subject boundary of one settings value, as the product declares it.

    The same two words the generated core settings contract uses
    (`service-template`, `docs/CONTRACTS.md`, "Core settings v1"): `product`
    stores one product-wide value, `user` requires a positive local
    `subject_id`.
    """

    PRODUCT = "product"
    USER = "user"


class InitialSetting(BaseModel):
    """One typed value the confirmed product is meant to start life with.

    Identified the way the generated product identifies it — a manifest-declared
    `key`, an explicit `scope`, and for a user-scoped value the positive local
    `subject_id` it belongs to — so that writing it later through the product's
    `settings.set` is a transcription and not an interpretation. The value is
    JSON; the schema it is validated against lives in the product's manifest,
    not here.

    A credential is never a setting: this brief is read back by the architect
    and therefore by an LLM, while a secret is resolved by Python at the
    execution boundary. A credential-shaped key or value is refused here rather
    than relied upon to be kept out by a prompt.
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=255)
    scope: SettingScope = SettingScope.PRODUCT
    subject_id: int | None = Field(default=None, ge=1)
    value: Any = None

    @field_validator("key")
    @classmethod
    def _key_is_a_manifest_key(cls, value: str) -> str:
        value = value.strip()
        if not SETTING_KEY_RE.match(value):
            raise ValueError(
                f"a settings key is a manifest-declared dotted lowercase name, not {value!r}"
            )
        lowered = value.lower()
        if any(fragment in lowered for fragment in _CREDENTIAL_KEY_FRAGMENTS):
            raise ValueError(
                "a credential is not a setting: store it with set_project_secret, "
                f"not in the Product Brief ({value!r})"
            )
        return value

    @model_validator(mode="after")
    def _scope_names_its_subject(self) -> InitialSetting:
        if self.scope is SettingScope.USER and self.subject_id is None:
            raise ValueError("a user-scoped setting needs a positive subject_id")
        if self.scope is SettingScope.PRODUCT and self.subject_id is not None:
            raise ValueError("a product-scoped setting has no subject_id")
        if isinstance(self.value, str) and _CREDENTIAL_VALUE_RE.match(self.value.strip()):
            raise ValueError(
                "a credential is not a setting: this value has the shape of a bot token"
            )
        return self


class MustRequirement(BaseModel):
    """One thing the product must do, addressed by id in every disposition.

    The read shape. `text` is the requirement as the brief states it, and the
    two optional fields say where that statement came from: `user_wording` is
    what the user actually wrote, `wording_reference` an auditable pointer to
    where they wrote it. Which of the two is present is a decision of the
    producer, and `ProposedMustRequirement` is where the producer is held to
    making it.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=10000)
    #: The user's own words for this requirement, verbatim.
    user_wording: str | None = Field(default=None, max_length=10000)
    #: Where the user's words are, when quoting them here is not the right
    #: place — e.g. `telegram:chat=42:message=1337`.
    wording_reference: str | None = Field(default=None, max_length=500)

    @field_validator("id", "text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("requirement fields must not be blank")
        return value

    @field_validator("user_wording", "wording_reference")
    @classmethod
    def _optional_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("a wording or reference that is present must not be blank")
        return value


class ProposedMustRequirement(MustRequirement):
    """A must-requirement as a producer may write it. Strict on both counts.

    Path-safe id, and exactly one provenance: the user's wording, or a reference
    to it. Neither is a paraphrase nobody can audit; both at once is two answers
    to the one question of where the requirement came from.
    """

    @field_validator("id")
    @classmethod
    def _id_is_path_safe(cls, value: str) -> str:
        value = value.strip()
        if not REQUIREMENT_ID_RE.match(value):
            raise ValueError(
                "a must-requirement id is addressed as one URL path segment; "
                f"{value!r} is not path-safe"
            )
        return value

    @model_validator(mode="after")
    def _exactly_one_provenance(self) -> ProposedMustRequirement:
        if bool(self.user_wording) == bool(self.wording_reference):
            raise ValueError(
                "a must-requirement carries the user's wording or a reference to it, not both "
                "and not neither"
            )
        return self


class ProductBriefContent(BaseModel):
    """The confirmed brief document. Frozen once `confirmed_at` is stamped.

    The read shape — what `ProductBriefRead` parses out of the JSON column.
    `initial_settings` defaults to empty, so a document stored before this field
    existed still parses as the same brief with nothing seeded.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=10000)
    must_requirements: list[MustRequirement] = Field(min_length=1)
    #: The typed values the product starts with, in the order the user was shown
    #: them. Ordered because the confirmation is one message and its order is
    #: part of what was confirmed.
    initial_settings: list[InitialSetting] = Field(default_factory=list)

    @model_validator(mode="after")
    def _requirement_ids_are_unique(self) -> ProductBriefContent:
        ids = [requirement.id for requirement in self.must_requirements]
        if len(ids) != len(set(ids)):
            raise ValueError("must requirement ids must be unique")
        subjects = [
            (setting.key, setting.scope, setting.subject_id) for setting in self.initial_settings
        ]
        if len(subjects) != len(set(subjects)):
            raise ValueError("initial settings must not name the same key, scope and subject twice")
        return self


class ProposedProductBriefContent(ProductBriefContent):
    """The brief document as a producer may write it. The write shape.

    Identical to the stored document field for field; it differs only in
    refusing what must never be opened as a revision in the first place.
    """

    must_requirements: list[ProposedMustRequirement] = Field(min_length=1)


class ProductBriefCreate(BaseModel):
    """Open a new revision of a project's brief. Never edits an existing one."""

    model_config = ConfigDict(extra="forbid")

    project_id: uuid.UUID
    title: str = Field(min_length=1, max_length=500)
    content: ProposedProductBriefContent
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
    content: ProposedProductBriefContent


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
    #: attempt id, and the same transaction voids what the superseded attempt
    #: planned, because nothing would ever release it.
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

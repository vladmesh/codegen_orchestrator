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

**Caps on a proposal, not on what is stored.** Every count and every text a
proposal carries is capped (the `MAX_*` constants below), so the brief's full
form stays under `shared.product_brief_text.FULL_BRIEF_CEILING` — far below the
20k-character brief that broke the 2026-09-25 canary. The caps sit on the write
shapes only; a revision stored before them still parses through the read shape.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
import re
from typing import Annotated, Any
import uuid

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from shared.contracts.dto.story_planning import PlanningChannels

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


#: What a proposed brief may carry at most. The worst case — every count and
#: every text at its cap — renders a full form under
#: `shared.product_brief_text.FULL_BRIEF_CEILING`; a product that needs more is
#: staged into several briefs, one story each. Read shapes keep their old,
#: looser limits, so a revision stored before these caps still loads.
MAX_BRIEF_TITLE_LENGTH = 100
MAX_SUMMARY_LENGTH = 400
MAX_MUST_REQUIREMENTS = 8
MAX_REQUIREMENT_TEXT_LENGTH = 200
MAX_USER_WORDING_LENGTH = 250
MAX_USAGE_EXAMPLES = 10
MAX_USER_SENDS_LENGTH = 150
MAX_PRODUCT_ANSWERS_LENGTH = 200
MAX_LIMITATIONS = 5
MAX_LIMITATION_LENGTH = 200
MAX_INITIAL_SETTINGS = 6
MAX_SETTING_DESCRIPTION_LENGTH = 150


class SettingScope(StrEnum):
    """The subject boundary of one settings value, as the product declares it.

    The same two words the generated core settings contract uses
    (`codegen-product-kit`, `docs/CONTRACTS.md`, "Core settings v1"): `product`
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
    #: What this setting and its chosen value mean, in the user's language — the
    #: only way the user is shown it. Absent on documents stored before it existed.
    description: str | None = Field(default=None, max_length=1000)

    @field_validator("description")
    @classmethod
    def _description_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("a setting description that is present must not be blank")
        return value

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
    #: Whether the user interacts with this requirement. A user-facing one is
    #: shown with at least one usage example; one that is only internal or
    #: scheduled with nothing the user sends may have none.
    user_facing: bool = True

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
    to the one question of where the requirement came from. The user's words
    are quoted up to a cap; longer ones are referenced instead.
    """

    text: str = Field(min_length=1, max_length=MAX_REQUIREMENT_TEXT_LENGTH)
    user_wording: str | None = Field(default=None, max_length=MAX_USER_WORDING_LENGTH)

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


class ProposedInitialSetting(InitialSetting):
    """A setting as a producer may write it: the user is shown its description."""

    description: str = Field(min_length=1, max_length=MAX_SETTING_DESCRIPTION_LENGTH)


#: A user's language as the brief names it: an ISO 639 code, optionally with a
#: region or script subtag — `ru`, `en`, `pt-br`.
LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})*$")


class UsageExample(BaseModel):
    """One exchange that shows the user how a must-requirement is used.

    Both sides are in the user's words: what they send (a text, a command, a
    button press, a photo — described, not encoded) and what the product
    answers.
    """

    model_config = ConfigDict(extra="forbid")

    requirement_id: str = Field(min_length=1, max_length=128)
    user_sends: str = Field(min_length=1, max_length=2000)
    product_answers: str = Field(min_length=1, max_length=2000)

    @field_validator("requirement_id", "user_sends", "product_answers")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("usage example fields must not be blank")
        return value


class ProposedUsageExample(UsageExample):
    """A usage example as a producer may write it: each side capped."""

    user_sends: str = Field(min_length=1, max_length=MAX_USER_SENDS_LENGTH)
    product_answers: str = Field(min_length=1, max_length=MAX_PRODUCT_ANSWERS_LENGTH)


class ProductBriefContent(BaseModel):
    """The confirmed brief document. Frozen once `confirmed_at` is stamped.

    The read shape — what `ProductBriefRead` parses out of the JSON column.
    Every field added after the first release defaults — `initial_settings`,
    `language`, `usage_examples`, `limitations` — so a document stored before
    it existed still parses as the same brief.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=10000)
    must_requirements: list[MustRequirement] = Field(min_length=1)
    #: The typed values the product starts with, in the order the user was shown
    #: them. Ordered because the confirmation is one message and its order is
    #: part of what was confirmed.
    initial_settings: list[InitialSetting] = Field(default_factory=list)
    #: The language the user is shown the brief in, e.g. `ru` or `en`.
    language: str | None = Field(default=None, max_length=35)
    #: How the user will use the product: at least one example per user-facing
    #: must-requirement, in the order the user was shown them.
    usage_examples: list[UsageExample] = Field(default_factory=list)
    #: Limitations and chosen trade-offs, one plain-language sentence each.
    limitations: list[str] = Field(default_factory=list)

    @field_validator("limitations")
    @classmethod
    def _limitations_not_blank(cls, value: list[str]) -> list[str]:
        stripped = [limitation.strip() for limitation in value]
        if not all(stripped):
            raise ValueError("a limitation must not be blank")
        return stripped

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
    refusing what must never be opened as a revision in the first place: a
    missing language, a setting the user could only be shown by its key, a usage
    example of a requirement the brief does not have, and a user-facing
    requirement nobody showed the user how to use. Every count and text is
    capped (`MAX_*`), which the stored document is not.
    """

    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_LENGTH)
    must_requirements: list[ProposedMustRequirement] = Field(
        min_length=1, max_length=MAX_MUST_REQUIREMENTS
    )
    initial_settings: list[ProposedInitialSetting] = Field(
        default_factory=list, max_length=MAX_INITIAL_SETTINGS
    )
    language: str = Field(min_length=2, max_length=35)
    usage_examples: list[ProposedUsageExample] = Field(
        default_factory=list, max_length=MAX_USAGE_EXAMPLES
    )
    limitations: list[Annotated[str, StringConstraints(max_length=MAX_LIMITATION_LENGTH)]] = Field(
        default_factory=list, max_length=MAX_LIMITATIONS
    )

    @field_validator("language")
    @classmethod
    def _language_is_a_code(cls, value: str) -> str:
        value = value.strip().lower()
        if not LANGUAGE_RE.match(value):
            raise ValueError(f"a language is an ISO 639 code such as 'ru' or 'en', not {value!r}")
        return value

    @model_validator(mode="after")
    def _every_user_facing_requirement_has_an_example(self) -> ProposedProductBriefContent:
        known = [requirement.id for requirement in self.must_requirements]
        unknown = sorted({example.requirement_id for example in self.usage_examples} - set(known))
        if unknown:
            raise ValueError(
                f"usage examples name unknown must-requirement ids: {', '.join(unknown)}"
            )
        exemplified = {example.requirement_id for example in self.usage_examples}
        missing = [
            requirement.id
            for requirement in self.must_requirements
            if requirement.user_facing and requirement.id not in exemplified
        ]
        if missing:
            raise ValueError(
                f"user-facing must-requirements have no usage example: {', '.join(missing)}"
            )
        return self


class ProductBriefCreate(BaseModel):
    """Open a new revision of a project's brief. Never edits an existing one."""

    model_config = ConfigDict(extra="forbid")

    project_id: uuid.UUID
    title: str = Field(min_length=1, max_length=MAX_BRIEF_TITLE_LENGTH)
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

    def planning_attempt_is_live(self, now: datetime) -> bool:
        """Is an architect still proving it owns this brief's incomplete plan?

        The question the API asks of the row before it hands a claim to a second
        architect, asked of the row the API returned: an active attempt whose
        heartbeat is within `PLANNING_ATTEMPT_HEARTBEAT_TIMEOUT_SECONDS`.
        """
        if not self.planning_attempt_active or self.planning_attempt_heartbeat_at is None:
            return False
        heartbeat = self.planning_attempt_heartbeat_at
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=UTC)
        return heartbeat >= now - timedelta(seconds=PLANNING_ATTEMPT_HEARTBEAT_TIMEOUT_SECONDS)


class ProductBriefFullText(BaseModel):
    """The full form of one revision, as `GET /product-briefs/{id}/full` returns it.

    One item per section, in reading order (`render_full_brief_sections`); the
    PO's `show_full_brief` joins the same sections with `MESSAGE_BREAK`.
    """

    model_config = ConfigDict(extra="forbid")

    brief_id: str
    revision: int
    language: str | None = None
    sections: list[str]


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


class ProductBriefAdmissionCommand(ProductBriefPlanningAttemptCommand, PlanningChannels):
    """The body of `admit`: the attempt, and the LLM channels that planned it.

    The admission that releases the plan records the story's `planned` outcome
    with these channels in the same transaction, so a released plan always
    says which channel planned it.
    """

    model_config = ConfigDict(extra="forbid")

    reopen: bool = False


#: How a `returned_reason` starts when the requirement was returned because no
#: check QA can perform observes it: the Architect's rule for a must-requirement
#: whose only usage needs an action QA does not have. What follows it names what
#: QA would need. A reason is free text otherwise; this is its one fixed form.
NOT_AUTOMATICALLY_VERIFIABLE_PREFIX = "not automatically verifiable:"


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

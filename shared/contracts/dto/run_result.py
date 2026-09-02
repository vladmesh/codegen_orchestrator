"""Typed result payloads stored in `Run.result`, keyed by `RunType`.

Each `RunType` has exactly one result shape. The models use `extra="forbid"`
so an unknown field or a payload belonging to another run type is rejected at
the boundary instead of being silently carried as a raw dict. Unknown enum
values (e.g. an outcome string the code doesn't know) fail validation for the
same reason. `RunDTO` binds the union to `RunType` and rejects a mismatched
pair — see `RunDTO._check_result_matches_type`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.contracts.dto.engineering import EngineeringStatus
from shared.contracts.dto.settings_seed import (
    SETTINGS_SEED_RETRYABLE_FAILURES,
    SettingSeedOutcome,
)
from shared.contracts.queues.deploy import DeployAction, DeployOutcome
from shared.contracts.queues.qa import QAOutcome


class AllocationFailureReason(StrEnum):
    """Stable admission failure classifications consumed by the scheduler."""

    INSUFFICIENT_FREE_MEMORY = "insufficient_free_memory"
    INSUFFICIENT_RESERVED_MEMORY = "insufficient_reserved_memory"
    IMPOSSIBLE_CAPACITY = "impossible_capacity"
    NO_FRESH_METRICS = "no_fresh_metrics"
    # No candidate host was an admissible target: an unfinished or broken build,
    # a status that does not admit, a host that is not managed. Whichever it was,
    # it is the platform's own state and not a capacity shortage, and the
    # scheduler must not describe it to a user as one — see
    # `shared/server_admission.py::ADMISSION_FAILURE_REASON`, the one reason every
    # admission refusal carries.
    SERVER_NOT_PROVISIONED = "server_not_provisioned"


class EngineeringRunResult(BaseModel):
    """Result of an engineering run (written by the engineering result handler)."""

    model_config = ConfigDict(extra="forbid")

    engineering_status: EngineeringStatus
    commit_sha: str | None = None
    selected_modules: list[str] | None = None
    test_results: dict | None = None
    allocation_failure_reason: AllocationFailureReason | None = None
    allocation_required_ram_mb: int | None = None
    allocation_min_disk_mb: int | None = None


class MissingUserSecret(BaseModel):
    """A required user secret the resolver could not find at deploy time.

    Carries the contract `key` and its human-facing `description` so the
    scheduler can ask the user for it by name without ever reading the secret
    value. `consumers` from the contract stays out of this on purpose — it is
    internal wiring the user does not need.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    description: str


class DeployRunResult(BaseModel):
    """Result of a deploy run.

    `deploy_outcome` is the routing field the scheduler reads. `deployment_result`
    and `smoke_result` are opaque diagnostic blobs from the DevOps subgraph; they
    are stored for observability and never routed on. `missing_user_secrets` is
    the structured list the scheduler reads on a WAITING_FOR_USER_SECRET outcome.

    A deploy that could not place the application carries the allocation
    classification across this boundary instead of erasing it into an error
    string: `allocation_failure_reason` and the admission budget the attempt
    asked for are what let the scheduler tell "the platform could not host this
    yet" from "this project is broken", and re-check admission before it tries
    again.
    """

    model_config = ConfigDict(extra="forbid")

    deploy_outcome: DeployOutcome
    deployed_url: str | None = None
    application_id: int | None = None
    bot_username: str | None = None
    deploy_fix_attempt: int = 0
    error_details: str | None = None
    missing_user_secrets: list[MissingUserSecret] = Field(default_factory=list)
    action: DeployAction | None = None
    deployment_result: dict | None = None
    smoke_result: dict | None = None
    allocation_failure_reason: AllocationFailureReason | None = None
    allocation_required_ram_mb: int | None = None
    allocation_min_disk_mb: int | None = None
    #: What became of each `initial_settings` value of the confirmed Product
    #: Brief backing this story — see `shared/contracts/dto/settings_seed.py`.
    #: Empty is the ordinary case: a standalone deploy, a story with no brief,
    #: or a brief that confirmed no settings.
    settings_seed: list[SettingSeedOutcome] = Field(default_factory=list)

    @model_validator(mode="after")
    def _infrastructure_wait_carries_its_classification(self) -> DeployRunResult:
        """A wait the scheduler cannot re-check is not a wait, it is a stall.

        `WAITING_INFRASTRUCTURE` exists so an allocation refusal keeps its type
        past this boundary. A producer that sets the outcome without the reason
        and the admission budget leaves the scheduler holding a story it can
        neither resume nor distinguish from a broken project, so the contract
        refuses it here rather than letting it be discovered in a supervisor tick.
        """
        if self.deploy_outcome is not DeployOutcome.WAITING_INFRASTRUCTURE:
            return self
        missing = [
            name
            for name, value in (
                ("allocation_failure_reason", self.allocation_failure_reason),
                ("allocation_required_ram_mb", self.allocation_required_ram_mb),
                ("allocation_min_disk_mb", self.allocation_min_disk_mb),
            )
            if value is None
        ]
        if missing:
            raise ValueError(
                f"{DeployOutcome.WAITING_INFRASTRUCTURE.value} requires {', '.join(missing)}"
            )
        return self

    @model_validator(mode="after")
    def _success_means_every_confirmed_setting_arrived(self) -> DeployRunResult:
        """A deploy is not a success while a confirmed setting is still missing.

        This is the one place the invariant lives, so that every producer and
        every reconciliation reaches it rather than each remembering to check:
        a result may not say SUCCESS while it also records a settings-seed
        failure that a second deploy of the same commit could answer
        differently. Such a run belongs to
        `DeployOutcome.SETTINGS_SEED_FAILED`, which goes round under a bound.

        The deterministic failures — an undeclared key, a schema-refused value,
        a pinned product whose contract predates the write capability — are
        deliberately not covered: no redeploy of this artifact changes them, so
        they are reported beside the successful deploy instead of blocking it.
        """
        if self.deploy_outcome is not DeployOutcome.SUCCESS:
            return self
        held_back = sorted(
            {
                outcome.failure.value
                for outcome in self.settings_seed
                if outcome.failure in SETTINGS_SEED_RETRYABLE_FAILURES
            }
        )
        if held_back:
            raise ValueError(
                f"{DeployOutcome.SUCCESS.value} cannot carry a settings-seed failure that holds "
                f"the deploy back ({', '.join(held_back)}); use "
                f"{DeployOutcome.SETTINGS_SEED_FAILED.value}"
            )
        return self


class QAFailedCheck(BaseModel):
    """A single failed QA check the scheduler turns into a fix-task line."""

    model_config = ConfigDict(extra="forbid")

    name: str
    detail: str


class QABlockerCategory(StrEnum):
    """Closed set of reasons QA could not make a product judgement."""

    MISSING_BOT_USERNAME = "missing_bot_username"
    MISSING_TELETHON_CREDENTIALS = "missing_telethon_credentials"
    # No executor could be started for exploratory QA: the assigned coding
    # agent's subscription session is missing, expired or broken, and the
    # optional API fallback is not configured either. This replaced
    # `claude_unavailable`, which had come to mean only "no LLM API key" — a
    # meaning that stopped being true once the executor became a subscription
    # CLI agent and the API triplet became an optional fallback.
    QA_EXECUTOR_UNAVAILABLE = "qa_executor_unavailable"
    DEPLOYED_URL_UNREACHABLE = "deployed_url_unreachable"
    # A deterministic pre-agent probe could not be performed at all: the target's
    # container runtime did not answer, or the platform API that holds the bot
    # token did not. Neither says anything about the product, and neither is
    # `server_unavailable` — that one means the run never got onto the host and
    # is repaired by looking at the host or its provisioning, while this one
    # means the platform is on the host (or on its own API) and the thing it
    # asked did not answer. Conflating them would make both unactionable.
    QA_PROBE_UNAVAILABLE = "qa_probe_unavailable"
    # Telegram answered, and the bot this deployment is bound to is not live:
    # the token was revoked, replaced or never bound. Distinct from
    # `telegram_access_denied`, which is a live bot refusing the QA account and
    # is repaired by the temporary-access mechanism; this one is repaired by
    # binding a working token, and no amount of test access changes it.
    BOT_NOT_LIVE = "bot_not_live"
    TELEGRAM_ACCESS_DENIED = "telegram_access_denied"
    # The QA runtime could not deliver the requested Telegram operation. This
    # is deliberately distinct from a bot reply that failed an acceptance
    # criterion: the product did not receive the operation, so no conclusion
    # about its behaviour can reach the engineering-fix loop.
    TELEGRAM_PROBE_UNDELIVERED = "telegram_probe_undelivered"
    SERVER_UNAVAILABLE = "server_unavailable"
    QA_CLEANUP_FAILED = "qa_cleanup_failed"
    # The temporary identity QA tests private bots with: never handed over, or
    # taken back while the run was still using it.
    QA_ACCESS_GRANT_FAILED = "qa_access_grant_failed"
    QA_ACCESS_EXPIRED = "qa_access_expired"
    UNKNOWN = "unknown"


class QABlocker(BaseModel):
    """Evidence that QA was blocked before it could judge the product.

    Unknown classifications deliberately remain blockers. Creating an unnecessary
    human-review item is cheaper than directing an engineering worker to alter a
    correct customer project from an ambiguous QA failure.
    """

    model_config = ConfigDict(extra="forbid")

    category: QABlockerCategory
    attempted: str
    sent: str
    received: str


class QATelegramReplyButton(BaseModel):
    """One button a Telegram reply made visible to the QA executor."""

    model_config = ConfigDict(extra="forbid")

    row: int = Field(ge=0)
    column: int = Field(ge=0)
    text: str | None = None
    type: str
    # Callback data is base64 encoded at the Telegram boundary. Its presence
    # makes an inline button actionable without exposing a Telegram session;
    # reply-keyboard buttons identify themselves by their row, column and text.
    callback_data: str | None = None


class QATelegramReplyMarkup(BaseModel):
    """The reply or inline keyboard carried by one bot reply."""

    model_config = ConfigDict(extra="forbid")

    type: str
    buttons: list[QATelegramReplyButton] = Field(default_factory=list)


class QATelegramReplyEvidence(BaseModel):
    """Observable fields of one Telegram message received from the bound bot."""

    model_config = ConfigDict(extra="forbid")

    id: int
    text: str | None = None
    caption: str | None = None
    media_type: str | None = None
    reply_markup: QATelegramReplyMarkup | None = None


class QATelegramCallbackEvidence(BaseModel):
    """Telegram's direct answer to a run-scoped inline-button callback."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    alert: bool = False
    url: str | None = None


class QATelegramProbeEvidence(BaseModel):
    """Runner-owned evidence for one Telegram message or callback operation."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["message", "callback"]
    attempted: str
    sent: str
    # None means the child process did not leave enough evidence to prove
    # whether delivery happened. It is still a blocker, never product evidence.
    delivered: bool | None = None
    replies: list[QATelegramReplyEvidence] = Field(default_factory=list)
    callback: QATelegramCallbackEvidence | None = None
    # Callback operations re-read the pressed bot reply after the press. This
    # makes an edit-in-place observable even when Telegram sends no new reply.
    post_press_message: QATelegramReplyEvidence | None = None
    error: str | None = None


class QAStateChangeCleanup(BaseModel):
    """The cleanup attempt for application state changed by QA."""

    model_config = ConfigDict(extra="forbid")

    attempted: bool
    succeeded: bool
    detail: str = Field(min_length=1)

    @model_validator(mode="after")
    def _successful_cleanup_was_attempted(self) -> QAStateChangeCleanup:
        if self.succeeded and not self.attempted:
            raise ValueError("successful QA cleanup must have been attempted")
        return self


class QAStateChangeOperation(StrEnum):
    """Kinds of application-state write traces retained in a QA result."""

    CREATED = "created"
    MODIFIED = "modified"


class QAStateChange(BaseModel):
    """An application-state trace retained with its cleanup evidence."""

    model_config = ConfigDict(extra="forbid")

    resource: str = Field(min_length=1)
    operation: QAStateChangeOperation
    cleanup: QAStateChangeCleanup


class QARunResult(BaseModel):
    """Result of a QA run (written by the QA consumer)."""

    model_config = ConfigDict(extra="forbid")

    qa_outcome: QAOutcome
    summary: str | None = None
    failed_checks: list[QAFailedCheck] = Field(default_factory=list)
    report: str | None = None
    qa_attempt: int | None = None
    deployed_url: str | None = None
    error: str | None = None
    blocker: QABlocker | None = None
    telegram_probe_evidence: list[QATelegramProbeEvidence] = Field(default_factory=list)
    state_changes: list[QAStateChange] = Field(default_factory=list)

    @model_validator(mode="after")
    def _outcome_matches_state_traces(self) -> QARunResult:
        if self.qa_outcome == QAOutcome.BLOCKED and self.blocker is None:
            raise ValueError("blocked QA outcome requires a blocker")
        if self.qa_outcome == QAOutcome.PASSED and any(
            not change.cleanup.succeeded for change in self.state_changes
        ):
            raise ValueError("passed QA outcome cannot contain an uncleaned state change")
        return self


RunResult = EngineeringRunResult | DeployRunResult | QARunResult

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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.contracts.dto.engineering import EngineeringStatus
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
    # Whether the deployed commit declares the contract slot a QA run borrows to
    # reach a private bot. The scheduler cannot see the generated repository, and
    # sending an undeclared override would fail the next deploy, so the deploy
    # that read the contract is what reports it.
    test_identity_slot: bool = False
    deploy_fix_attempt: int = 0
    error_details: str | None = None
    missing_user_secrets: list[MissingUserSecret] = Field(default_factory=list)
    action: DeployAction | None = None
    deployment_result: dict | None = None
    smoke_result: dict | None = None
    allocation_failure_reason: AllocationFailureReason | None = None
    allocation_required_ram_mb: int | None = None
    allocation_min_disk_mb: int | None = None

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


class QAFailedCheck(BaseModel):
    """A single failed QA check the scheduler turns into a fix-task line."""

    model_config = ConfigDict(extra="forbid")

    name: str
    detail: str


class QABlockerCategory(StrEnum):
    """Closed set of reasons QA could not make a product judgement."""

    MISSING_BOT_USERNAME = "missing_bot_username"
    MISSING_TELETHON_CREDENTIALS = "missing_telethon_credentials"
    CLAUDE_UNAVAILABLE = "claude_unavailable"
    DEPLOYED_URL_UNREACHABLE = "deployed_url_unreachable"
    TELEGRAM_ACCESS_DENIED = "telegram_access_denied"
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

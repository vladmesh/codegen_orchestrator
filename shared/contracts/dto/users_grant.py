"""Durable, non-secret intents for generated-service permanent access."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.contracts.git_ref import CommitSha

USERS_GRANT_INTENT_KEY = "users_grant_intent"


class GrantIntentKind(StrEnum):
    INITIAL_OWNER = "initial_owner"
    ADD_USER = "add_user"
    INCOMING_OWNER = "incoming_owner"


class GrantIntentLifecycleRequest(BaseModel):
    """Internal admission; a PR number selects trusted persisted merge evidence."""

    model_config = ConfigDict(extra="forbid")

    kind: GrantIntentKind
    story_id: str | None = None
    head_sha: CommitSha | None = None
    deployed_commit_sha: CommitSha | None = None
    merged_pr_number: int | None = Field(default=None, gt=0)
    # Internal recovery of a committed, still-owed immutable Run. Never opens
    # an epoch or refreshes the deliberate human retry fence.
    expected_execution_run_id: str | None = Field(default=None, min_length=1, max_length=255)


class GrantIntentStatus(StrEnum):
    PUBLISH_OWED = "publish_owed"
    QUEUED = "queued"
    APPLYING = "applying"
    APPLIED = "applied"
    RETRYABLE = "retryable"
    FAILED = "failed"


class GrantIntentLifecycleDisposition(StrEnum):
    """What this lifecycle call did with a permanent-access intent.

    A durable intent can retain its current execution reference for worker
    completion checks and audit. That reference is never evidence that this
    particular call dispatched work: only ``DISPATCHED`` carries a new attempt.
    """

    DISPATCHED = "dispatched"
    ALREADY_APPLIED = "already_applied"
    IN_FLIGHT = "in_flight"
    EXHAUSTED = "exhausted"
    STALE_TARGET = "stale_target"


class GrantIntentDispatchTarget(BaseModel):
    """The immutable target bound to the deploy Run created by this call."""

    model_config = ConfigDict(extra="forbid")

    application_id: int | None = None
    deployment_id: int | None = None
    sha: CommitSha


class GrantIntentRetryCommand(BaseModel):
    """Deliberate human command, fenced by the exhausted immutable attempt."""

    model_config = ConfigDict(extra="forbid")

    expected_execution_run_id: str = Field(min_length=1, max_length=255)


class GrantIntentExhaustion(BaseModel):
    """Safe readback of a bounded initial-owner deployment epoch."""

    model_config = ConfigDict(extra="forbid")

    code: Literal["initial_owner_deployment_exhausted"] = "initial_owner_deployment_exhausted"
    attempts: int = Field(ge=0)
    target: GrantIntentDispatchTarget
    exhausted_execution_run_id: str | None
    action: Literal["retry_initial_owner_deployment"] | None
    retry_command: GrantIntentRetryCommand | None

    @model_validator(mode="after")
    def _action_requires_current_run_fence(self) -> "GrantIntentExhaustion":
        if (self.action is None) != (self.retry_command is None):
            raise ValueError("exhaustion action and retry command must agree")
        if self.retry_command is not None and (
            self.exhausted_execution_run_id != self.retry_command.expected_execution_run_id
            or self.attempts == 0
        ):
            raise ValueError("retry command requires the exhausted admitted Run")
        return self


class GrantIntentLifecycleResult(BaseModel):
    """Per-call result of creating or resuming a grant-intent lifecycle."""

    model_config = ConfigDict(extra="forbid")

    intent_id: str = Field(min_length=1, max_length=255)
    status: GrantIntentStatus
    disposition: GrantIntentLifecycleDisposition
    execution_run_id: str | None = None
    target: GrantIntentDispatchTarget | None = None
    created: bool = False
    exhaustion: GrantIntentExhaustion | None = None

    @model_validator(mode="after")
    def _dispatch_owns_its_attempt(self) -> "GrantIntentLifecycleResult":
        if self.disposition is GrantIntentLifecycleDisposition.DISPATCHED:
            if self.execution_run_id is None or self.target is None:
                raise ValueError("dispatched grant intent requires its run and immutable target")
        elif self.execution_run_id is not None or self.target is not None:
            raise ValueError("only dispatched grant intent may carry an execution run or target")
        return self


class GrantIntent(BaseModel):
    """One idempotent request to grant a verified external identity.

    Stored in a deploy Run's durable metadata. It intentionally excludes
    capability material, bot tokens, decrypted project secrets, and audiences.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=255)
    kind: GrantIntentKind
    project_id: str = Field(min_length=1)
    channel: str = Field(min_length=1, max_length=64)
    external_id: str = Field(min_length=1, max_length=255)
    target_application_id: int | None = None
    target_deployment_id: int | None = None
    target_sha: CommitSha
    initiating_actor: str = Field(min_length=1, max_length=255)
    outgoing_owner_id: int | None = None
    incoming_owner_id: int | None = None
    status: GrantIntentStatus = GrantIntentStatus.PUBLISH_OWED
    attempts: int = Field(default=0, ge=0)
    detail: str | None = Field(default=None, max_length=512)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    applied_at: datetime | None = None
    execution_run_id: str | None = None
    target_history: list[dict[str, object]] = Field(default_factory=list)
    retry_history: list[dict[str, object]] = Field(default_factory=list)
    exhaustion: GrantIntentExhaustion | None = None

    def with_status(self, status: GrantIntentStatus, *, detail: str | None = None) -> "GrantIntent":
        """Return a safe state transition without changing the target."""
        updates: dict[str, object] = {"status": status, "detail": detail}
        if status is GrantIntentStatus.APPLIED:
            updates["applied_at"] = datetime.now(UTC)
        return self.model_copy(update=updates)

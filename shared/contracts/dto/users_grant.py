"""Durable, non-secret intents for generated-service permanent access."""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.contracts.git_ref import CommitSha

USERS_GRANT_INTENT_KEY = "users_grant_intent"


class GrantIntentKind(StrEnum):
    INITIAL_OWNER = "initial_owner"
    ADD_USER = "add_user"
    INCOMING_OWNER = "incoming_owner"


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


class GrantIntentDispatchTarget(BaseModel):
    """The immutable target bound to the deploy Run created by this call."""

    model_config = ConfigDict(extra="forbid")

    application_id: int | None = None
    deployment_id: int | None = None
    sha: CommitSha


class GrantIntentLifecycleResult(BaseModel):
    """Per-call result of creating or resuming a grant-intent lifecycle."""

    model_config = ConfigDict(extra="forbid")

    intent_id: str = Field(min_length=1, max_length=255)
    status: GrantIntentStatus
    disposition: GrantIntentLifecycleDisposition
    execution_run_id: str | None = None
    target: GrantIntentDispatchTarget | None = None
    created: bool = False

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

    def with_status(self, status: GrantIntentStatus, *, detail: str | None = None) -> "GrantIntent":
        """Return a safe state transition without changing the target."""
        updates: dict[str, object] = {"status": status, "detail": detail}
        if status is GrantIntentStatus.APPLIED:
            updates["applied_at"] = datetime.now(UTC)
        return self.model_copy(update=updates)

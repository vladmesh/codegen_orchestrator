"""Publication facts are separate from the immutable paid attempt outcome."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shared.diagnostics import redact_diagnostic

COMMIT_PUBLICATION_KEY = "commit_publication"
SHA_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"


def publication_pending_key(attempt_id: str) -> str:
    return f"engineering:publication-pending:{attempt_id}"


class PublicationFailure(StrEnum):
    BRANCH_MISSING = "branch_missing"
    WRONG_BRANCH = "wrong_branch"
    WRONG_REPOSITORY = "wrong_repository"
    OBJECT_MISSING = "object_missing"
    HEAD_CHANGED = "head_changed"
    INSPECTION_FAILED = "inspection_failed"
    INJECTED_PATHS = "injected_paths"
    NO_NEW_COMMIT = "no_new_commit"
    PUSH_REFUSED = "push_refused"
    READBACK_MISMATCH = "readback_mismatch"
    TIMEOUT = "timeout"
    OWNERSHIP_MISSING = "ownership_missing"
    STALE_ATTEMPT = "stale_attempt"
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"


class CommitPublication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    published: bool = False
    commit_sha: str | None = Field(default=None, pattern=SHA_PATTERN)
    branch: str | None = Field(default=None, min_length=1)
    worker_id: str | None = Field(default=None, min_length=1)
    attempt_id: str | None = Field(default=None, min_length=1)
    repository_id: str | None = Field(default=None, min_length=1)
    repository_url: str | None = Field(default=None, min_length=1)
    remote_sha: str | None = Field(default=None, pattern=SHA_PATTERN)
    failure: PublicationFailure | None = None
    stderr: str = ""

    @field_validator("stderr", mode="before")
    @classmethod
    def bounded_stderr(cls, value):
        return redact_diagnostic(value)[:2000]

    @model_validator(mode="after")
    def truthful_receipt(self):
        if self.published:
            if not self.commit_sha or self.remote_sha != self.commit_sha or self.failure:
                raise ValueError("publication requires exact remote proof and no failure")
        elif self.failure is None:
            raise ValueError("refused publication requires a failure")
        return self


class CommitRecoveryCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: str = Field(min_length=1)
    commit_sha: str = Field(pattern=SHA_PATTERN)
    # Deliberate adoption is never the runtime interpretation of a legacy result.
    adopt_preserved_commit: bool = False
    # Names the current committed stop; another or newer stop never clears.
    stop_id: str | None = Field(default=None, min_length=1)


class EngineeringStop(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    stopped_at: datetime
    released_at: datetime | None = None
    release_actor: str | None = None


class AttemptDisposition(StrEnum):
    STOPPED = "stopped"
    PUBLICATION_REQUIRED = "publication_required"
    ELIGIBLE = "eligible"


class AttemptDispositionRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    disposition: AttemptDisposition
    project_id: str
    story_id: str | None
    attempt_id: str
    initiating_run_id: str | None


class CommitRecoveryIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    story_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    initiating_run_id: str = Field(min_length=1)
    repository_id: str = Field(min_length=1)
    repository_url: str = Field(min_length=1)
    branch: str = Field(min_length=1)
    baseline: str = Field(pattern=SHA_PATTERN)
    cycle: str | None
    iteration: int | None


class CommitRecoveryRead(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    attempt_id: str
    story_id: str
    commit_sha: str
    actor: str
    stop_id: str | None
    claimed_at: datetime
    receipt: CommitPublication | None
    handed_off_at: datetime | None
    identity: CommitRecoveryIdentity

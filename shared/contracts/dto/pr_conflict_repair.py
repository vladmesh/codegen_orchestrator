"""One bounded engineering Task for a dirty PR in a stable story cycle."""

from datetime import UTC, datetime
from enum import StrEnum
import hashlib
import uuid

from pydantic import BaseModel, ConfigDict, Field

PR_CONFLICT_REPAIR_KEY = "pr_conflict_repair"


def cycle_stamp(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def repair_task_id(story_id: str, cycle: datetime) -> str:
    identity = f"{story_id}:{cycle_stamp(cycle).isoformat()}"
    return "pr-conflict-" + hashlib.sha256(identity.encode()).hexdigest()[:32]


class PRConflictRepairCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: uuid.UUID
    pr_number: int = Field(gt=0)
    cycle_started_at: datetime
    expected_head_sha: str | None = Field(default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class PRConflictRepairEvidence(PRConflictRepairCommand):
    story_id: str
    repository_id: str
    head_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    default_branch: str = Field(min_length=1)
    default_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    max_iterations: int = Field(gt=0)


class PRConflictRepairOutcome(StrEnum):
    ADMITTED = "admitted"
    REUSED = "reused"
    EXHAUSTED = "exhausted"


class PRConflictRepairRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: PRConflictRepairOutcome
    story_id: str
    task_id: str
    pr_number: int
    max_iterations: int
    reason: str | None = None

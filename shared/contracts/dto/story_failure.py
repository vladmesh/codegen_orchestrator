"""Why a story stopped, in a form its owner's PO and an operator can both read.

A story that stops because the platform could not do its part — the repository
was never scaffolded, the scaffold never finished — used to stop silently: the
failing service logged the cause and left the story in ``in_progress``, where
the PO read "still being built" for as long as anybody asked. The cause lived
only in a log line and in ``projects.config.scaffold_error``, neither of which
the story carried.

``StoryFailure`` is the one shape such a stop records on the story. It is
written into ``stories.quarantine_reason`` by the same API action that fails or
parks the story (``POST /stories/{id}/fail`` or ``/human-review`` with a
``failure`` body), together with the owed owner record, in one transaction. The
detail is redacted and bounded when the model is built, so no caller can
persist a secret or an unbounded traceback through it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from shared.diagnostics import redact_diagnostic

#: The project config key the scaffolder records a failed scaffold/ensure under.
SCAFFOLD_ERROR_KEY = "scaffold_error"

#: The ``quarantine_reason.reason`` value a ``StoryFailure`` is stored under.
STORY_FAILURE_REASON = "story_failure"

#: The longest detail a story carries. Long enough for a git or copier error
#: line, short enough that it is never a traceback.
STORY_FAILURE_DETAIL_LIMIT = 500


def bounded_diagnostic(value: object, limit: int = STORY_FAILURE_DETAIL_LIMIT) -> str:
    """Redact and truncate one diagnostic string for a reader outside the service."""
    text = redact_diagnostic(value).strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


class StoryFailureCode(StrEnum):
    """What stopped the story."""

    #: The scaffolder recorded ``projects.config.scaffold_error``: the project
    #: repository could not be created, so no work on it can start.
    SCAFFOLD_FAILED = "scaffold_failed"
    #: The architect waited for the scaffold for its whole window and the project
    #: never left ``draft``, with no recorded error to explain it.
    SCAFFOLD_TIMEOUT = "scaffold_timeout"


class StoryFailure(BaseModel):
    """The typed reason a failed or blocked story carries in ``quarantine_reason``."""

    model_config = ConfigDict(extra="forbid")

    reason: Literal["story_failure"] = STORY_FAILURE_REASON
    code: StoryFailureCode
    #: The service that decided the stop (``scaffolder``, ``architect``).
    source: str = Field(min_length=1, max_length=64)
    #: The cause, as the failing service saw it. Redacted and bounded here.
    detail: str
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("detail", mode="before")
    @classmethod
    def _bounded_detail(cls, value: object) -> str:
        return bounded_diagnostic(value)


_OWNER_WORDS: dict[StoryFailureCode, str] = {
    StoryFailureCode.SCAFFOLD_FAILED: (
        "Work on this change stopped before it began: the platform could not create the "
        "project's code repository, so nothing was built."
    ),
    StoryFailureCode.SCAFFOLD_TIMEOUT: (
        "Work on this change stopped before it began: preparing the project's code "
        "repository did not finish in time, so nothing was built."
    ),
}


def story_failure_owner_text(failure: StoryFailure) -> str:
    """The owed owner message for a stop: what happened, the cause, what comes next."""
    return (
        f"{_OWNER_WORDS[failure.code]} Cause reported by the {failure.source}: "
        f"{failure.detail} This is a platform problem, not something the user did. "
        "Nothing more happens automatically; a person has to fix it before the change "
        "can be tried again."
    )


#: Task statuses that close a task for good. A closed task from before a reopen
#: is history, not a plan for the current work cycle.
CLOSED_TASK_STATUSES = frozenset({"done", "cancelled"})


def in_work_cycle(created_at: datetime, reopened_at: datetime | None, status: str) -> bool:
    """Whether a task is part of the story's current plan.

    A reopened story starts a new cycle at ``reopened_at``; a task created
    since is part of it. So is any task still open, whenever it was created —
    the CI-retry move creates its fix task just before it stamps the reopen.
    Only a closed task from before the reopen is history.
    """
    if reopened_at is None or status not in CLOSED_TASK_STATUSES:
        return True
    created = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
    reopened = reopened_at if reopened_at.tzinfo else reopened_at.replace(tzinfo=UTC)
    return created >= reopened


def story_failure_admin_text(story_id: str, project_id: str, failure: StoryFailure) -> str:
    """The administrator notice for the same stop."""
    return (
        f"Story {story_id} (project {project_id}) stopped: {failure.code.value} "
        f"from {failure.source}: {failure.detail}"
    )


# --- Diagnostics a story's PO may read ---

#: The most log lines one diagnostics read returns.
STORY_DIAGNOSTIC_LOG_LIMIT = 20
#: The most failed runs and task failure events one diagnostics read returns.
STORY_DIAGNOSTIC_RECORD_LIMIT = 5
#: How far back the log search looks, and its hard ceiling.
STORY_DIAGNOSTIC_LOG_HOURS_DEFAULT = 6
STORY_DIAGNOSTIC_LOG_HOURS_MAX = 72
#: The longest single log field a diagnostics read returns.
STORY_DIAGNOSTIC_LOG_FIELD_LIMIT = 300


class StoryDiagnosticRun(BaseModel):
    """One failed Run of the story, reduced to what explains the failure."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    status: str
    error: str | None
    completed_at: datetime | None = None


class StoryDiagnosticTaskEvent(BaseModel):
    """One task status change into a failure or a stop."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    to_status: str | None
    actor: str
    created_at: datetime
    #: The event's details, serialized, redacted and bounded.
    details: str | None


class StoryDiagnosticLogLine(BaseModel):
    """One error or warning log line about the story or its project.

    Only named fields are copied out of the structured line; everything else it
    carried is dropped, so nothing the service logged beside the error leaves.
    """

    model_config = ConfigDict(extra="forbid")

    timestamp: str | None
    service: str | None
    level: str | None
    event: str | None
    error: str | None


class StoryDiagnosticsRead(BaseModel):
    """What a story's owner may be told about why it is where it is.

    ``GET /api/stories/{id}/diagnostics``. Read-only, bounded and redacted:
    every free-text field went through ``bounded_diagnostic``. ``logs`` is
    empty and ``logs_unavailable`` says why when the log store could not be
    read — the database part never depends on it.
    """

    model_config = ConfigDict(extra="forbid")

    story_id: str
    project_id: str
    story_status: str
    project_status: str
    #: The typed reason the story stopped, when a platform failure stopped it.
    failure: StoryFailure | None = None
    #: The raw ``quarantine_reason`` for any other stop (QA, state-age bound…),
    #: serialized, redacted and bounded.
    quarantine_reason: str | None = None
    #: ``projects.config.scaffold_error``: the project repository failed.
    scaffold_error: str | None = None
    #: Tasks in the story's current work cycle.
    work_cycle_tasks: int
    failed_runs: list[StoryDiagnosticRun] = []
    task_failures: list[StoryDiagnosticTaskEvent] = []
    logs: list[StoryDiagnosticLogLine] = []
    logs_unavailable: str | None = None

"""How the architect's last planning of a story ended, kept on the story itself.

A planning attempt that raised used to leave nothing behind but a log line: the
architect released its Product Brief attempt and returned, the story stayed
``in_progress`` with the failed attempt's unadmitted tasks, and a replay skipped
it as "already decomposed" (canary 2, story-92b433c8, an OpenRouter 402). The
story read as work in progress with no error for as long as anybody asked.

``StoryPlanning`` is the record of the outcome, written by
``POST /stories/{id}/planning-outcome`` on the locked story row:

* ``planned`` — the attempt succeeded; the record names the LLM channels that
  planned it, so "which channel planned this story" needs no log search;
* ``retrying`` — the attempt failed with a failure another try may clear; the
  scheduler supervisor re-queues planning once ``next_attempt_at`` passes;
* ``parked`` — the retries ran out, or the failure is one no retry can clear;
  the same write moved the story to ``waiting_human_review`` with a
  ``planning_failed`` ``StoryFailure`` and owed the owner and admin notices.
  ``POST /stories/{id}/retry-planning`` is the operator's way back.

``failed_attempts`` is the durable retry count: it lives on the story row, so
neither a scheduler restart nor a lost Redis key resets it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.contracts.dto.story_failure import StoryFailure, StoryFailureCode

#: The system config key bounding automatic planning retries. The same bound
#: the supervisor applies to a story stuck in ``created``: one number says how
#: many times the platform re-runs the architect on its own.
PLANNING_MAX_RETRIES_CONFIG_KEY = "supervisor.story_max_architect_retries"

#: The first retry waits this long; every further one waits twice the previous.
#: With the default bound of 3 that is 1, 2 and 4 minutes — long enough for a
#: provider's rate limit or 5xx to clear, and well inside the hour after which
#: the planless-story bound would stop the story.
PLANNING_RETRY_BACKOFF_SECONDS = 60

#: The most channel entries one outcome carries. A chain has a handful of
#: channels; a planning run retries them per model call, so the lists are
#: bounded rather than trusted.
PLANNING_CHANNEL_LIST_LIMIT = 32


def planning_retry_delay(failed_attempts: int) -> timedelta:
    """How long the retry after the ``failed_attempts``-th failure waits."""
    return timedelta(seconds=PLANNING_RETRY_BACKOFF_SECONDS * 2 ** max(failed_attempts - 1, 0))


class StoryPlanningState(StrEnum):
    """Where the story's planning stands after the last recorded attempt."""

    PLANNED = "planned"
    RETRYING = "retrying"
    PARKED = "parked"


class StoryPlanningOutcome(StrEnum):
    """What the architect reports about one planning attempt."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


#: One channel entry: a channel name, or ``channel:failure_class``.
_ChannelEntry = Annotated[str, Field(min_length=1, max_length=64)]


class _PlanningChannels(BaseModel):
    """The LLM channels one planning attempt used, as `ChannelUsage` reported them."""

    #: Channels that answered, in order of first use (``codex``, ``openrouter``).
    channels: list[_ChannelEntry] = Field(
        default_factory=list, max_length=PLANNING_CHANNEL_LIST_LIMIT
    )
    #: Channels that failed a call, as ``channel:failure_class``.
    channel_failures: list[_ChannelEntry] = Field(
        default_factory=list, max_length=PLANNING_CHANNEL_LIST_LIMIT
    )


class StoryPlanningReport(_PlanningChannels):
    """The body of ``POST /stories/{id}/planning-outcome``: one attempt's outcome."""

    model_config = ConfigDict(extra="forbid")

    actor: str = Field(default="architect", min_length=1, max_length=64)
    outcome: StoryPlanningOutcome
    #: Why the attempt failed. Required for ``failed``, refused for ``succeeded``.
    failure: StoryFailure | None = None
    #: False when no retry can clear the failure (every channel refused
    #: payment, credentials or quota): the story is parked at once.
    retriable: bool = True
    #: The Product Brief planning attempt the run held, if the story is brief-backed.
    planning_attempt_id: str | None = Field(default=None, max_length=128)
    #: Whether the run planned a reopen, so a retry re-sends the same kind of job.
    reopen: bool = False

    @model_validator(mode="after")
    def _failure_matches_outcome(self) -> StoryPlanningReport:
        if self.outcome is StoryPlanningOutcome.FAILED:
            if self.failure is None:
                raise ValueError("a failed planning outcome needs its failure")
            if self.failure.code is not StoryFailureCode.PLANNING_FAILED:
                raise ValueError("a failed planning outcome is a planning_failed failure")
        elif self.failure is not None:
            raise ValueError("a succeeded planning outcome carries no failure")
        return self


class StoryPlanning(_PlanningChannels):
    """The outcome of the story's last planning attempt, as ``stories.planning`` holds it."""

    model_config = ConfigDict(extra="forbid")

    state: StoryPlanningState
    #: Failed attempts since the last success or operator re-run.
    failed_attempts: int = Field(ge=0)
    #: The retry bound in force when the failure was recorded; ``None`` for a success.
    max_retries: int | None = Field(default=None, ge=0)
    #: When the supervisor may re-queue planning; set only while ``retrying``.
    next_attempt_at: datetime | None = None
    #: The failure of the last attempt; ``None`` for a success.
    last_failure: StoryFailure | None = None
    planning_attempt_id: str | None = Field(default=None, max_length=128)
    reopen: bool = False
    recorded_at: datetime


def planned_record(report: StoryPlanningReport, now: datetime) -> StoryPlanning:
    """The record a successful attempt leaves: the channels that planned the story."""
    return StoryPlanning(
        state=StoryPlanningState.PLANNED,
        failed_attempts=0,
        planning_attempt_id=report.planning_attempt_id,
        reopen=report.reopen,
        recorded_at=now,
        channels=report.channels,
        channel_failures=report.channel_failures,
    )


def failed_record(
    previous: StoryPlanning | None,
    report: StoryPlanningReport,
    *,
    max_retries: int,
    now: datetime,
) -> StoryPlanning:
    """The record a failed attempt leaves: another retry, or the park.

    The count continues only from a story that is still retrying; a success or
    an operator's re-run starts it again. A retriable failure within the bound
    is scheduled after `planning_retry_delay`; anything else is ``parked``.
    """
    failed_attempts = 1 + (
        previous.failed_attempts
        if previous is not None and previous.state is StoryPlanningState.RETRYING
        else 0
    )
    retrying = report.retriable and failed_attempts <= max_retries
    return StoryPlanning(
        state=StoryPlanningState.RETRYING if retrying else StoryPlanningState.PARKED,
        failed_attempts=failed_attempts,
        max_retries=max_retries,
        next_attempt_at=now + planning_retry_delay(failed_attempts) if retrying else None,
        last_failure=report.failure,
        planning_attempt_id=report.planning_attempt_id,
        reopen=report.reopen,
        recorded_at=now,
        channels=report.channels,
        channel_failures=report.channel_failures,
    )

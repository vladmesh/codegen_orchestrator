"""Ending one expired Story wait, only if the wait is still the one that expired.

The state-age watchdog decides that a wait is over from what it read: the
story's status and the anchor its age is measured from. Between that read and
the ending, routing may already have moved the story on — a deploy that
reported, a re-dispatch that made a new Run, a merge, a saved secret. Ending the
wait anyway would park or fail a story that is progressing, and nothing about
the target status stops it, because the new status often allows the same
transition.

So the ending is a compare-and-set. The watchdog sends what it observed — the
status it expected, the identity of the anchor — together with everything the
ending writes, and the API compares both against the locked rows before it
writes anything. It either writes the typed reason, the owed owner record and
the transition in one transaction, or writes nothing and names the mismatch.
The comparison is `StateWaitExpiryCommand.mismatch`, a pure function of the
command and what the locked rows show, so the API and every test decide it the
same way.

The pull request's ``updated_at`` lives on GitHub, not in a row the API can
lock. For ``pr_review`` the API checks ``pr_number``; the watchdog re-reads the
pull request immediately before the call and skips if it moved
(`StateWaitSkipReason.PR_UPDATED_AT_MOVED`), which is the closest check an
external anchor allows.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.contracts.dto.owner_notification import OwnerNotification, OwnerNotificationState
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.story import StoryStatus
from shared.contracts.vocab import OwnerNotificationEvent

#: The typed reason a story carries when a state age bound ended its wait.
STATE_AGE_BOUND_REASON = "state_wait_age_bound_exceeded"


class StateWaitEnding(StrEnum):
    """How an expired wait ends.

    ``PARK`` hands the story to the human-review queue: the platform could not
    finish, which is not evidence that the product is broken. ``FAIL`` is for a
    wait whose subject is outside the platform and simply never came — the only
    honest ending, and the only one ``waiting_user_secret`` even has a
    transition for.
    """

    PARK = "park"
    FAIL = "fail"


TERMINAL_STATUS_BY_ENDING: dict[StateWaitEnding, StoryStatus] = {
    StateWaitEnding.PARK: StoryStatus.WAITING_HUMAN_REVIEW,
    StateWaitEnding.FAIL: StoryStatus.FAILED,
}

OWNER_EVENT_BY_ENDING: dict[StateWaitEnding, OwnerNotificationEvent] = {
    StateWaitEnding.PARK: OwnerNotificationEvent.STORY_BLOCKED,
    StateWaitEnding.FAIL: OwnerNotificationEvent.STORY_FAILED,
}

#: The Run whose latest row anchors a status' wait, where the anchor is a Run.
#: ``pr_review`` is the one bounded status whose anchor is not.
ANCHOR_RUN_TYPE_BY_STATUS: dict[StoryStatus, RunType] = {
    StoryStatus.DEPLOYING: RunType.DEPLOY,
    StoryStatus.TESTING: RunType.QA,
    StoryStatus.WAITING_USER_SECRET: RunType.DEPLOY,
}

#: A Run in one of these statuses is still a wait. A terminal Run is an outcome
#: the deploy/QA supervisors route, never something to bound.
IN_FLIGHT_RUN_STATUSES = frozenset({RunStatus.QUEUED, RunStatus.RUNNING})

_BOUNDED_STATUSES = frozenset({*ANCHOR_RUN_TYPE_BY_STATUS, StoryStatus.PR_REVIEW})


class StateWaitSkipReason(StrEnum):
    """Why a wait the watchdog saw expire was not ended."""

    #: The story is no longer in the status the watchdog read.
    STATUS_MOVED = "status_moved"
    #: The story's latest Run of the anchor type is not the anchored Run.
    RUN_REPLACED = "run_replaced"
    #: The anchored Run reached a terminal status; routing owns its outcome.
    RUN_TERMINAL = "run_terminal"
    #: The secret request is no longer delivered at the anchored moment.
    ASK_RECORD_CHANGED = "ask_record_changed"
    #: Every secret the wait was for is saved; routing resumes the deploy.
    SECRETS_SAVED = "secrets_saved"  # noqa: S105
    #: The story now names a different pull request.
    PR_NUMBER_CHANGED = "pr_number_changed"
    #: The pull request moved on GitHub after the watchdog read it. Decided by
    #: the watchdog's re-read before the call; the API never returns it.
    PR_UPDATED_AT_MOVED = "pr_updated_at_moved"


class StateWaitSkip(BaseModel):
    """One named mismatch between what the watchdog saw and what is there now."""

    model_config = ConfigDict(extra="forbid")

    reason: StateWaitSkipReason
    expected: str | None
    actual: str | None


class StateWaitAnchor(BaseModel):
    """The identity of the anchor a wait's age was measured from.

    ``run_id`` for a Run-anchored status, plus ``ask_delivered_at`` for
    ``waiting_user_secret``; ``pr_number`` for ``pr_review``.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str | None = Field(default=None, min_length=1)
    ask_delivered_at: datetime | None = None
    pr_number: int | None = Field(default=None, ge=1)


class StateWaitExpiryReason(BaseModel):
    """The typed ``quarantine_reason`` an ended wait leaves on its story."""

    model_config = ConfigDict(extra="forbid")

    reason: Literal["state_wait_age_bound_exceeded"] = STATE_AGE_BOUND_REASON
    status: StoryStatus
    waiting_on: str
    config_key: str
    threshold_minutes: int
    anchor: str
    anchor_at: str
    age_minutes: float
    ending: StateWaitEnding

    def names_same_wait(self, stored: dict | None) -> bool:
        """True when ``stored`` is this reason for the same wait, however old it grew."""
        if not isinstance(stored, dict):
            return False
        mine = self.model_dump(mode="json")
        return all(
            stored.get(key) == mine[key] for key in ("reason", "status", "anchor", "anchor_at")
        )


class StateWaitObservation(BaseModel):
    """What the locked rows show, in the terms the comparison needs.

    ``run_*`` and ``ask`` describe the story's latest Run of the anchor type;
    ``secrets_saved`` is whether every secret that Run reported missing is now
    stored on the project.
    """

    model_config = ConfigDict(extra="forbid")

    status: StoryStatus
    pr_number: int | None = None
    run_id: str | None = None
    run_status: RunStatus | None = None
    ask: OwnerNotification | None = None
    secrets_saved: bool = False


class StateWaitExpiryCommand(BaseModel):
    """End one expired wait, only if the story still is where the watchdog saw it."""

    model_config = ConfigDict(extra="forbid")

    expected_status: StoryStatus
    ending: StateWaitEnding
    anchor: StateWaitAnchor
    reason: StateWaitExpiryReason
    owner_notification: OwnerNotification

    @model_validator(mode="after")
    def _is_one_consistent_ending(self) -> StateWaitExpiryCommand:
        if self.expected_status not in _BOUNDED_STATUSES:
            raise ValueError(f"{self.expected_status.value} is not a bounded wait")
        if self.expected_status is StoryStatus.PR_REVIEW:
            if self.anchor.pr_number is None:
                raise ValueError("a pr_review wait is anchored on its pr_number")
        elif self.anchor.run_id is None:
            raise ValueError(f"a {self.expected_status.value} wait is anchored on a run_id")
        if (
            self.expected_status is StoryStatus.WAITING_USER_SECRET
            and self.anchor.ask_delivered_at is None
        ):
            raise ValueError("a waiting_user_secret wait is anchored on its ask's delivered_at")
        if self.reason.status is not self.expected_status or self.reason.ending is not self.ending:
            raise ValueError("the reason names another wait or ending")
        record = self.owner_notification
        if (
            record.terminal_status is not self.terminal_status
            or record.event is not OWNER_EVENT_BY_ENDING[self.ending]
            or record.state is not OwnerNotificationState.OWED
        ):
            raise ValueError("the owner notification is not the owed notice of this ending")
        return self

    @property
    def terminal_status(self) -> StoryStatus:
        return TERMINAL_STATUS_BY_ENDING[self.ending]

    def is_repeat(self, status: str, quarantine_reason: dict | None) -> bool:
        """True when this very wait has already been ended by an earlier call."""
        return status == self.terminal_status.value and self.reason.names_same_wait(
            quarantine_reason
        )

    def mismatch(self, seen: StateWaitObservation) -> StateWaitSkip | None:
        """The first way the story differs from what was observed, or None."""
        expected = self.expected_status
        if seen.status is not expected:
            return _skip(StateWaitSkipReason.STATUS_MOVED, expected.value, seen.status.value)
        if expected is StoryStatus.PR_REVIEW:
            if seen.pr_number != self.anchor.pr_number:
                return _skip(
                    StateWaitSkipReason.PR_NUMBER_CHANGED,
                    str(self.anchor.pr_number),
                    None if seen.pr_number is None else str(seen.pr_number),
                )
            return None
        if seen.run_id != self.anchor.run_id:
            return _skip(StateWaitSkipReason.RUN_REPLACED, self.anchor.run_id, seen.run_id)
        if expected is StoryStatus.WAITING_USER_SECRET:
            return self._ask_mismatch(seen)
        if seen.run_status not in IN_FLIGHT_RUN_STATUSES:
            return _skip(
                StateWaitSkipReason.RUN_TERMINAL,
                "|".join(sorted(status.value for status in IN_FLIGHT_RUN_STATUSES)),
                None if seen.run_status is None else seen.run_status.value,
            )
        return None

    def _ask_mismatch(self, seen: StateWaitObservation) -> StateWaitSkip | None:
        ask = seen.ask
        delivered_at = self.anchor.ask_delivered_at
        if (
            ask is None
            or ask.event is not OwnerNotificationEvent.STORY_WAITING_USER_SECRET
            or ask.state is not OwnerNotificationState.DELIVERED
            or ask.delivered_at != delivered_at
        ):
            actual = (
                None
                if ask is None
                else f"{ask.event.value}:{ask.state.value}:"
                f"{None if ask.delivered_at is None else ask.delivered_at.isoformat()}"
            )
            return _skip(
                StateWaitSkipReason.ASK_RECORD_CHANGED,
                f"{OwnerNotificationEvent.STORY_WAITING_USER_SECRET.value}:"
                f"{OwnerNotificationState.DELIVERED.value}:{delivered_at.isoformat()}",
                actual,
            )
        if seen.secrets_saved:
            return _skip(StateWaitSkipReason.SECRETS_SAVED, "missing", "saved")
        return None


def _skip(reason: StateWaitSkipReason, expected: str | None, actual: str | None) -> StateWaitSkip:
    return StateWaitSkip(reason=reason, expected=expected, actual=actual)


class StateWaitExpiryDisposition(StrEnum):
    """What one expire-state-wait call committed."""

    #: This call wrote the reason, the owed owner record and the transition.
    EXPIRED = "expired"
    #: An earlier call already ended this very wait; nothing was written.
    ALREADY_ENDED = "already_ended"
    #: The story is not where the watchdog saw it; nothing was written.
    SKIPPED = "skipped"


class StateWaitExpiryRead(BaseModel):
    """The API's answer to one expire-state-wait call."""

    model_config = ConfigDict(extra="forbid")

    disposition: StateWaitExpiryDisposition
    story_id: str
    story_status: StoryStatus
    skip: StateWaitSkip | None = None

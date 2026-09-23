"""The record that the owner of a story is owed a message.

A terminal story outcome — finished, or stopped for a human — is decided by the
supervisor and told to the owner through `po:input`. The transition is committed
to the database; the message is an `xadd` with nothing behind it. Publishing
after the commit therefore has a gap: if the publish, or the recipient lookup in
front of it, fails transiently, the story has already left the status the
supervisor scans and no tick ever looks at it again. The owner's product is
finished and nobody tells them, forever.

So the message is not inferred from a successful publish. A `story_completed`
record is committed on the Story with `COMPLETED`; other terminal notices are
written before their transition on the Run that produced them. From that moment
the record owns delivery. `OWED` means "the owner has not been told and must
be"; only a publish that returned moves it to `DELIVERED`.

The record carries the `terminal_status` the transition produces, and nothing is
published until the story is read and found in it. This protects run-backed
records whose transition failed and lets a completion whose response was lost
deliver safely from its committed Story record.

Three endings are not retries and must not look like one. `UNADDRESSABLE` is a
recipient that resolved to no Telegram chat: a refusal that is logged and
alerted once, because retrying it changes nothing. `ABANDONED` is a transient
failure that used up its attempts: the system gave up and a human was called.
`VOIDED` is the intended transition not being there — the obligation is settled
without publishing anything and without spending an attempt, and it is owed
again from scratch if routing later does finish the story.

This is deliberately narrow. It is not an outbox for every producer in the
project — it covers the terminal owner notifications the supervisor emits, which
are the ones whose story is unreachable the moment the transition lands, and the
lifecycle waits whose announcement is equally lost to a failed publish: a task
parked for, or resumed from, a resource wait, and a story waiting for a user
secret. Those are written by the API in the transaction of the state change
they announce, and a task-level one also names the task statuses in which it is
still true (`expected_task_statuses`). Progress notices
(`NON_DURABLE_OWNER_EVENTS`) stay outside it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.vocab import NON_DURABLE_OWNER_EVENTS, OwnerNotificationEvent

#: JSON key for run-backed terminal notices; completed-story notices live on Story.
OWNER_NOTIFICATION_KEY = "owner_notification"

#: The least time between two delivery attempts on one record. The API grants an
#: attempt only when this much has passed since ``last_attempt_at``, so the
#: spacing is a fact of the record, not of which loop happens to call first or of
#: how often it runs. A minute lets the transient failures this record survives —
#: an API restart, a Redis failover — clear between attempts, so the bounded
#: attempts are not all spent inside one outage.
OWNER_NOTIFICATION_ATTEMPT_INTERVAL = timedelta(seconds=60)

#: The ``detail.code`` of the 409 the API answers to a write carrying an older
#: attempt than the one the record holds: a visit that outlived its claim, whose
#: record has since been claimed again, may not overwrite what the newer visit
#: settled.
OWNER_NOTIFICATION_ATTEMPT_SUPERSEDED = "owner_notification_attempt_superseded"


class OwnerNotificationState(StrEnum):
    """What is known about the message this story owes its owner."""

    #: Written before the terminal transition. The owner has not been told.
    OWED = "owed"
    #: The event was accepted by `po:input`. Nothing publishes it again.
    DELIVERED = "delivered"
    #: The recipient resolved to no Telegram chat. A logged refusal, not a retry.
    UNADDRESSABLE = "unaddressable"
    #: Transient failures used up the bounded attempts; administrators were told.
    ABANDONED = "abandoned"
    #: The transition this record was written for is not in the story. Nothing
    #: was published and no attempt was spent; the obligation is owed again from
    #: scratch when the story really does reach that ending.
    VOIDED = "voided"


class OwnerNotification(BaseModel):
    """One terminal owner notification, and what is known about delivering it.

    The words are stored, not recomputed: the recovery pass must be able to
    publish exactly the message the tick that owed it decided on, without
    re-deriving it from a story whose state has moved on. The recipient is not
    stored — it is resolved at each attempt, because a lookup that failed
    transiently is one of the two failures this record exists to survive.
    """

    model_config = ConfigDict(extra="forbid")

    #: The `POSystemEvent.event` name PO routes on.
    event: OwnerNotificationEvent
    text: str
    story_id: str
    project_id: str
    #: The status the story must be in for this message to be true. Recorded
    #: rather than inferred so the check is the transition that was intended,
    #: not "any status that is not the one it started from".
    terminal_status: StoryStatus
    #: What PO is told the message is about, when that is a task rather than the
    #: story as a whole. `None` means the story is the subject, which is what
    #: every story-level ending records.
    task_id: str | None = None
    #: The statuses the task named by ``task_id`` must be in for this message to
    #: be true, checked at delivery next to ``terminal_status``. A notice about a
    #: task's lifecycle — "waiting for capacity", "resumed" — is made false by the
    #: task moving on while the story stays where it was, so the story alone
    #: cannot say whether it may still be published. `None` on every story-level
    #: ending and on every record written before this field existed: those keep
    #: exactly the story check.
    expected_task_statuses: tuple[TaskStatus, ...] | None = None
    state: OwnerNotificationState
    owed_at: datetime
    #: When the owner audience was marked delivered: the moment `po:input`
    #: accepted the event. `None` until then, and on every record delivered
    #: before this field existed. A wait that is measured from the owner having
    #: been told reads this, never `owed_at` — owing is not telling.
    delivered_at: datetime | None = None
    #: Delivery attempts already spent. Bounded by the producer.
    attempts: int = Field(default=0, ge=0)
    #: Why the last attempt did not deliver.
    detail: str | None = None
    #: When the last delivery attempt on this record was granted, whichever
    #: audience it served. Stamped by the API in the same locked write that
    #: checks ``OWNER_NOTIFICATION_ATTEMPT_INTERVAL``, and never by a caller.
    #: `None` means never attempted, which is what every record written before
    #: this field existed reads as. One stamp spaces both audiences because one
    #: granted visit serves both: two callers can never split a record's
    #: audiences between them and overwrite each other's settlement.
    last_attempt_at: datetime | None = None
    #: The administrator audience of the same ending, settled independently of
    #: the owner. Absent (`None`) on every record written before this audience
    #: existed and on endings that owe administrators nothing, so a released
    #: record keeps exactly its owner meaning and never gains an obligation.
    admin_text: str | None = None
    admin_state: OwnerNotificationState | None = None
    admin_attempts: int = Field(default=0, ge=0)
    admin_detail: str | None = None

    @model_validator(mode="after")
    def _event_is_durable(self) -> OwnerNotification:
        # A progress notice is told once or not at all. Letting it be owed would
        # hand the recovery sweep a message that is stale by the time it lands.
        if self.event in NON_DURABLE_OWNER_EVENTS:
            raise ValueError(f"{self.event} is never an owed owner notification")
        return self

    @model_validator(mode="after")
    def _task_expectation_names_a_task(self) -> OwnerNotification:
        if self.expected_task_statuses is not None and (
            self.task_id is None or not self.expected_task_statuses
        ):
            raise ValueError("expected_task_statuses needs a task_id and at least one status")
        return self

    @model_validator(mode="after")
    def _admin_audience_is_whole(self) -> OwnerNotification:
        if (self.admin_text is None) != (self.admin_state is None):
            raise ValueError("admin_text and admin_state are present together or not at all")
        return self

    @property
    def owed(self) -> bool:
        """True while somebody still has to publish this message to the owner."""
        return self.state is OwnerNotificationState.OWED

    @property
    def admin_owed(self) -> bool:
        """True while administrators still have to be told about this ending."""
        return self.admin_state is OwnerNotificationState.OWED

    def attempt_due(self, now: datetime) -> bool:
        """True when some audience is owed and the last attempt is an interval old."""
        if not self.owed and not self.admin_owed:
            return False
        return (
            self.last_attempt_at is None
            or now - self.last_attempt_at >= OWNER_NOTIFICATION_ATTEMPT_INTERVAL
        )

    def supersedes(self, incoming: OwnerNotification) -> bool:
        """True when ``incoming`` is a write from an attempt older than this record's.

        The same obligation is recognised by its ``owed_at``: a record owed
        afresh — a voided ending that became real, or a later lifecycle notice
        on the same Run — is a new obligation and replaces this one whatever it
        carries. The converse is refused: a write naming an obligation older
        than the stored one comes from a visit to a record this one replaced,
        and letting it land would put the replaced message back in its place.
        """
        if incoming.owed_at < self.owed_at:
            return True
        if incoming.owed_at != self.owed_at or self.last_attempt_at is None:
            return False
        return incoming.last_attempt_at is None or incoming.last_attempt_at < self.last_attempt_at


class OwnerNotificationAttemptClaim(BaseModel):
    """The API's answer to "may one delivery attempt be made on this record now?".

    ``granted`` means the record was stamped with this attempt in the same locked
    write that found it owed and due, and ``notification`` is the stamped record
    the attempt must carry into every write it makes. A refusal spends nothing
    and changes nothing; ``notification`` is then the record as it stands (a
    settled one, or one attempted less than an interval ago), or `None` when the
    source carries no record at all.
    """

    model_config = ConfigDict(extra="forbid")

    granted: bool
    notification: OwnerNotification | None

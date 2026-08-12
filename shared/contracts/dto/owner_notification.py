"""The record that the owner of a story is owed a message, written before they are.

A terminal story outcome — finished, or stopped for a human — is decided by the
supervisor and told to the owner through `po:input`. The transition is committed
to the database; the message is an `xadd` with nothing behind it. Publishing
after the commit therefore has a gap: if the publish, or the recipient lookup in
front of it, fails transiently, the story has already left the status the
supervisor scans and no tick ever looks at it again. The owner's product is
finished and nobody tells them, forever.

So the message is not inferred from a successful publish. It is written down
*before* the transition, on the QA run the outcome came from — the same place the
deploy already leaves its `qa_handoff` plan and the QA run its `qa_ssh_grant` —
and from that moment the record owns the delivery. `OWED` means "the owner has
not been told and must be"; only a publish that returned moves it to
`DELIVERED`. Anything else stays for the sweep, which retries a bounded number
of times and then alerts an administrator with the identifiers.

Two endings are not retries and must not look like one. `UNADDRESSABLE` is a
recipient that resolved to no Telegram chat: a refusal that is logged and
alerted once, because retrying it changes nothing. `ABANDONED` is a transient
failure that used up its attempts: the system gave up and a human was called.

This is deliberately narrow. It is not an outbox for every producer in the
project — it covers the terminal owner notifications the supervisor emits, which
are the ones whose story is unreachable the moment the transition lands.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

#: run_metadata key the record lives under, alongside `qa_handoff`.
OWNER_NOTIFICATION_KEY = "owner_notification"


class OwnerNotificationState(StrEnum):
    """What is known about the message this run's story owes its owner."""

    #: Written before the terminal transition. The owner has not been told.
    OWED = "owed"
    #: The event was accepted by `po:input`. Nothing publishes it again.
    DELIVERED = "delivered"
    #: The recipient resolved to no Telegram chat. A logged refusal, not a retry.
    UNADDRESSABLE = "unaddressable"
    #: Transient failures used up the bounded attempts; administrators were told.
    ABANDONED = "abandoned"


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
    event: str
    text: str
    story_id: str
    project_id: str
    state: OwnerNotificationState
    owed_at: datetime
    #: Delivery attempts already spent. Bounded by the producer.
    attempts: int = Field(default=0, ge=0)
    #: Why the last attempt did not deliver.
    detail: str | None = None

    @property
    def owed(self) -> bool:
        """True while somebody still has to publish this message."""
        return self.state is OwnerNotificationState.OWED

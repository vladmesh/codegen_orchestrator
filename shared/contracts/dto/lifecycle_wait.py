"""Entering and leaving a lifecycle wait, together with the owner notice it owes.

Three non-terminal moves are announced to the owner: a task parked in
``waiting_resources`` (for capacity, or for a server still being built), the
same task resumed from it, and a story parked in ``waiting_user_secret``. Each
used to be a transition followed by a best-effort publish, so a transient
Redis or recipient failure after the commit lost the announcement for good —
nothing scans for a wait that was entered but never told.

Each move is now one API action. On the locked rows it applies the transition
and writes the owed ``OwnerNotification`` on the Run the move was decided on,
in one transaction, and answers with a typed disposition and the record that
now owns the delivery. The scheduler supplies only the words; the API mints
the record's facts — story, project, task, the story status and task statuses
in which the message is true, and ``owed_at`` — from the rows it locked, so the
record cannot describe a state that did not commit with it. Delivery is then
the owner-notification seam's, like every terminal ending.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.contracts.dto.owner_notification import OwnerNotification
from shared.contracts.dto.run_result import AllocationFailureReason
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.vocab import OwnerNotificationEvent

#: The two announcements of a task entering the resource wait: a capacity
#: shortage, and a server whose software build has not finished.
RESOURCE_WAIT_EVENTS = frozenset(
    {
        OwnerNotificationEvent.TASK_WAITING_RESOURCES,
        OwnerNotificationEvent.TASK_WAITING_INFRASTRUCTURE,
    }
)

#: Where a task must be for "engineering is waiting" to be true.
RESOURCE_WAIT_TASK_STATUSES: tuple[TaskStatus, ...] = (TaskStatus.WAITING_RESOURCES,)

#: Where a task must be for "engineering has resumed" to be true: released to
#: the dispatcher, or already picked up by it. A task that went back to waiting,
#: to a human, or on past engineering has made the announcement stale.
RESOURCES_RESUMED_TASK_STATUSES: tuple[TaskStatus, ...] = (TaskStatus.TODO, TaskStatus.IN_DEV)


class TaskResourceWaitCommand(BaseModel):
    """``POST /api/tasks/{id}/park-waiting-resources``: park a refused engineering task.

    ``run_id`` is the refused engineering Run the park was decided on; the owed
    notice goes on it. The allocation facts are the ones the wait's own re-check
    reads. The notice is owed only when the park starts a new wait — the API
    decides that on the locked task, from whether its ``failure_metadata``
    already carries a wait start — so a wait is announced once, however many
    refused attempts it spans.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    allocation_failure_reason: AllocationFailureReason
    allocation_required_ram_mb: int | None = None
    allocation_min_disk_mb: int | None = None
    event: OwnerNotificationEvent
    text: str = Field(min_length=1)
    actor: str = Field(min_length=1)

    @model_validator(mode="after")
    def _event_announces_a_wait(self) -> TaskResourceWaitCommand:
        if self.event not in RESOURCE_WAIT_EVENTS:
            raise ValueError(f"{self.event} does not announce a resource wait")
        return self


class TaskResourceWaitDisposition(StrEnum):
    #: The task moved to ``waiting_resources`` in this transaction.
    PARKED = "parked"
    #: The task was already waiting: a repeat of a park whose answer was lost.
    #: Nothing was written.
    ALREADY_WAITING = "already_waiting"


class TaskResourceWaitRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disposition: TaskResourceWaitDisposition
    task_id: str
    run_id: str
    task_status: TaskStatus
    #: True when this park started the wait, which is when the notice is owed.
    new_wait: bool
    #: The notice record on ``run_id`` when it is one of this wait's, else None.
    owner_notification: OwnerNotification | None = None


class TaskResourceResumeCommand(BaseModel):
    """``POST /api/tasks/{id}/resume-from-resource-wait``: release a parked task.

    The notice goes on the task's latest engineering Run — the refused Run the
    park was decided on, since no new Run exists before the next dispatch — and
    replaces the wait's record there, delivered or not: a still-owed "waiting"
    message is superseded by the "resumed" one instead of arriving stale.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    actor: str = Field(min_length=1)


class TaskResourceResumeDisposition(StrEnum):
    #: The task went ``waiting_resources`` → ``backlog`` → ``todo`` in this transaction.
    RESUMED = "resumed"
    #: The task is not waiting any more — resumed, timed out or cancelled by a
    #: move this caller did not see. Nothing was written.
    NOT_WAITING = "not_waiting"


class TaskResourceResumeRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disposition: TaskResourceResumeDisposition
    task_id: str
    task_status: TaskStatus
    run_id: str | None = None
    owner_notification: OwnerNotification | None = None


class UserSecretWaitCommand(BaseModel):
    """``POST /api/stories/{id}/park-waiting-user-secret``: ask the owner for secrets.

    ``run_id`` is the deploy Run that reported the missing secrets; the ask is
    the record of exactly that wait and lives on it, where the state-age
    watchdog reads its ``delivered_at``.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    actor: str = Field(min_length=1)


class UserSecretWaitDisposition(StrEnum):
    #: The story moved to ``waiting_user_secret`` in this transaction.
    WAITING = "waiting"
    #: The story was already waiting: a repeat whose answer was lost. Nothing
    #: was written.
    ALREADY_WAITING = "already_waiting"


class UserSecretWaitRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disposition: UserSecretWaitDisposition
    story_id: str
    story_status: StoryStatus
    run_id: str
    #: The ask the Run carries: the one this move owed, or the one an earlier
    #: entry into the same wait already owed.
    owner_notification: OwnerNotification | None = None

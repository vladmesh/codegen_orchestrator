"""The one seam every terminal owner notification from the supervisor goes through.

The invariant it holds: a terminal story transition cannot be observed without
the owner's message being either already published to ``po:input`` or durably
owed. Nothing here is best-effort. `complete_story` writes the `story_completed`
record on the Story in its completion transaction; ``owe_owner_notification``
writes other terminal notices on the Run that produced them before their
transition. ``deliver_owed_notification`` is the only thing that settles either
record, whether called by the completing tick or a recovery sweep.

Why the record and not just a careful publish: the publish is an ``xadd`` with
nothing behind it, and the transition in front of it is a commit. Publish after
commit and a transient failure loses the message forever, because the story has
left the status the supervisor scans and no later tick sees it. Publish before
commit and a failure to commit leaves a user told about an outcome that did not
happen. The record removes the choice — it is written first, the transition is
committed, and the delivery is retried from the record until it lands or a human
is called.

Written first, the record is not evidence that the transition happened, so the
delivery does not treat it as such: it reads the story and publishes only if the
story is in the ``terminal_status`` the record was written for. Without that,
this seam would trade a lost message for a false one — a record committed on a
run whose story transition then failed would tell the owner their product is
finished while it is still in testing. The same check covers the opposite
failure for free: a transition that committed and lost its response leaves the
story terminal, so its message is delivered.

The lifecycle waits go through the same seam for the same reason: a task parked
in ``waiting_resources`` or resumed from it, and a story parked in
``waiting_user_secret``, are announced once, and nothing scans for a wait whose
announcement was lost. Their records are not written ahead of the transition —
the API action that makes the move writes the owed record on the Run it was
decided on, in the move's own transaction (``/tasks/{id}/park-waiting-resources``,
``/tasks/{id}/resume-from-resource-wait``, ``/stories/{id}/park-waiting-user-secret``),
so a failed move leaves no record and a committed one always has its record.
A task-level record also names the task statuses it is true in, and the
delivery checks those too: a "waiting" record whose task has already resumed is
voided, never published, and the resume's record replaces it on the Run.

Four endings, and they are deliberately not interchangeable:

* delivered — ``po:input`` accepted the event; nothing publishes it again.
* unaddressable — the owner resolved to no Telegram chat. Retrying that changes
  nothing, so it is a logged, alerted refusal and the record is settled.
* abandoned — transient failures used up the bounded attempts. An administrator
  is told, with the story, project, event and the record's source.
* voided — the intended transition is not in the story. Nothing is published,
  no attempt is spent, and the obligation is written again from scratch if
  routing later does reach that ending.

Attempts are spaced by the record, not by the order of the loops that make
them. Before anything is read or published, a visit asks the API for the attempt
(``claim_*_owner_notification_attempt``); the API grants it only when the record
still owes an audience and its ``last_attempt_at`` is at least
``OWNER_NOTIFICATION_ATTEMPT_INTERVAL`` old, and stamps it in the same locked
write. A refused visit spends nothing and publishes nothing. So routing's
in-tick attempt and the recovery sweep may run in either order, or at the same
moment, and a record still gets one attempt per interval. Every write the
granted visit makes carries its stamp; a visit that outlived its claim, while a
newer one claimed the record, is refused its writes and stops.

Delivery is at-least-once, not exactly-once. A process that dies between the
publish landing and the record being marked delivered republishes on the next
tick. That is the honest trade for never losing the message; what the record
does guarantee is that a *settled* notification is never published again.

The truth check has the same kind of window, and it is narrowed, not closed.
The story (and, for a task-level record, the task) is read a last time after
the recipient is resolved and immediately before the ``XADD``, so nothing slow
sits between the check and the publish. What remains is the moment between
those reads and Redis accepting the entry: a move committed inside it — a
resume landing just as a "waiting" notice is published — is not seen, and that
message goes out although its state has just ended. The record cannot retract
it; the newer record's own delivery still follows. So the seam does not promise
ordering between two notices about the same task: a "resumed" can reach PO
before a "waiting" published inside that moment. Closing it would take
publication coordinated with the state change across the API and PO, which
this seam does not have.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import httpx
import structlog

from shared.contracts.dto.owner_notification import (
    OWNER_NOTIFICATION_ATTEMPT_INTERVAL,
    OWNER_NOTIFICATION_ATTEMPT_SUPERSEDED,
    OWNER_NOTIFICATION_KEY,
    OwnerNotification,
    OwnerNotificationAttemptClaim,
    OwnerNotificationState,
)
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.po import POSystemEvent, to_flat_fields
from shared.contracts.vocab import OwnerNotificationEvent
from shared.notifications import (
    AdminDeliveryStatus,
    deliver_to_admins,
    notify_admins_best_effort,
)
from shared.queues import PO_INPUT_QUEUE
from shared.redis import RedisStreamClient

from ._recipients import resolve_project_recipient

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient

logger = structlog.get_logger(__name__)

#: Publishes one message may cost before the owner is declared undeliverable and
#: an administrator is called. Counted on the record, so a process that dies
#: mid-delivery cannot restart the count — the same reason the transport leg
#: counts its deliveries on Redis' PEL rather than in memory.
OWNER_NOTIFICATION_MAX_ATTEMPTS = 3

#: Runs the recovery sweep takes per cycle. The selection drains by itself: every
#: visit either delivers a record, spends one of its bounded attempts, or is
#: refused because the last attempt is less than an interval old, and such a
#: record is attempted again once the interval has passed.
OWNER_NOTIFICATION_PAGE = 100


class OwnerNotificationOutcome(StrEnum):
    """How one visit to an owed notification ended."""

    DELIVERED = "delivered"
    #: This attempt failed transiently and attempts remain. Still being chased.
    RETRYING = "retrying"
    #: Attempts are used up. Given up on, and a human was called.
    EXHAUSTED = "exhausted"
    #: There is no chat to deliver to. Refused, not retried.
    UNADDRESSABLE = "unaddressable"
    #: The transition this message was owed for is not in the story. Settled
    #: without publishing and without spending an attempt.
    VOIDED = "voided"
    #: The record was already settled by somebody else. Nothing was published.
    SKIPPED = "skipped"
    #: The API refused the attempt: the record was attempted less than
    #: ``OWNER_NOTIFICATION_ATTEMPT_INTERVAL`` ago. Nothing spent, nothing published.
    NOT_DUE = "not_due"
    #: This visit outlived its claim and a newer attempt claimed the record; the
    #: API refused this visit's write and it stopped. The newer attempt owns it.
    SUPERSEDED = "superseded"


class _AttemptSuperseded(Exception):
    """The API refused a write because a newer attempt holds the record."""


def _empty_counts() -> dict[str, int]:
    return {outcome.value: 0 for outcome in OwnerNotificationOutcome}


def _source_log_fields(source_id: str, *, story_record: bool) -> dict[str, str]:
    """Name the durable record's actual home in diagnostics.

    A completion record lives on its story, not on the QA run that happened to
    lead there. Keeping that distinction in alerts prevents an operator from
    looking up a story id as though it were a run id.
    """
    if story_record:
        return {"notification_source": "story", "source_id": source_id}
    return {"notification_source": "run", "run_id": source_id}


async def _write_record(
    api_client: SchedulerAPIClient, run_id: str, record: OwnerNotification
) -> None:
    """Put the record on the run. ``run_metadata`` is merged by the API."""
    await api_client.update_run(
        run_id, {"run_metadata": {OWNER_NOTIFICATION_KEY: record.model_dump(mode="json")}}
    )


async def _write_story_record(
    api_client: SchedulerAPIClient, story_id: str, record: OwnerNotification
) -> None:
    """Put the completion record on its story, where every completion route can create it."""
    await api_client.update_story_owner_notification(story_id, record.model_dump(mode="json"))


def read_owner_notification(run) -> OwnerNotification | None:
    """The record this run carries, or None if it was never owed one.

    A record that does not parse is not treated as absent and not treated as
    delivered: it raises. Only this module writes these, so an unreadable one is
    a defect in the code, and guessing which side of the invariant it falls on
    is exactly the guess this card exists to remove.
    """
    stored = run.run_metadata.get(OWNER_NOTIFICATION_KEY)
    if stored is None:
        return None
    return OwnerNotification.model_validate(stored)


def read_story_owner_notification(story) -> OwnerNotification | None:
    """The completion record this story carries, or None when it owes nothing."""
    stored = story.owner_notification
    if stored is None:
        return None
    return OwnerNotification.model_validate(stored)


async def owe_owner_notification(
    api_client: SchedulerAPIClient,
    run,
    *,
    event: OwnerNotificationEvent,
    text: str,
    story_id: str,
    project_id: str,
    terminal_status: StoryStatus,
    task_id: str | None = None,
    log: structlog.stdlib.BoundLogger,
) -> OwnerNotification:
    """Write down that the owner is owed this message. Call before the transition.

    ``terminal_status`` is the status the transition about to be committed puts
    the story in, and it is what the delivery checks before publishing anything.
    It is passed in rather than derived here because only the caller knows which
    transition it is about to make.

    Returns the record that now owns the delivery, which is the existing one
    when there already is one: a tick repeating a transition it already made
    must not reset a delivery in flight, and must not owe a second copy of a
    message that has already been delivered. A *voided* record is the exception
    — its transition never happened, so this is not a repeat, it is the first
    time this ending is real.
    """
    existing = read_owner_notification(run)
    if existing is not None and existing.state is not OwnerNotificationState.VOIDED:
        log.info(
            "owner_notification_already_recorded",
            run_id=run.id,
            po_event=existing.event,
            state=existing.state.value,
            attempts=existing.attempts,
        )
        return existing

    record = OwnerNotification(
        event=event,
        text=text,
        story_id=story_id,
        project_id=project_id,
        terminal_status=terminal_status,
        task_id=task_id,
        state=OwnerNotificationState.OWED,
        owed_at=datetime.now(UTC),
    )
    await _write_record(api_client, run.id, record)
    log.info(
        "owner_notification_owed",
        run_id=run.id,
        po_event=event,
        story_id=story_id,
        terminal_status=terminal_status.value,
        reowed=existing is not None,
    )
    return record


def new_story_owner_notification(
    story_id: str,
    *,
    event: OwnerNotificationEvent,
    text: str,
    project_id: str,
    terminal_status: StoryStatus,
) -> OwnerNotification:
    """A fresh owed story record, not yet written anywhere.

    For an ending whose API action writes the record in the transition's own
    transaction; `owe_story_owner_notification` writes it ahead of one instead.
    """
    return OwnerNotification(
        event=event,
        text=text,
        story_id=story_id,
        project_id=project_id,
        terminal_status=terminal_status,
        state=OwnerNotificationState.OWED,
        owed_at=datetime.now(UTC),
    )


async def owe_story_owner_notification(
    api_client: SchedulerAPIClient,
    story_id: str,
    *,
    event: OwnerNotificationEvent,
    text: str,
    project_id: str,
    terminal_status: StoryStatus,
    log: structlog.stdlib.BoundLogger,
) -> OwnerNotification:
    """Write down that the owner is owed this message, on the story. Call before
    the transition.

    The run-backed form above cannot serve every terminal ending, because not
    every ending has a Run. The PR poller decides two of them before anything is
    dispatched: a merge whose images were never published, and a story branch
    whose CI kept failing the same way until the fix budget ran out. Both take
    the story to human review and neither has a Run to hang the record on, so
    the record goes on the story — the same place the completion transaction
    puts one, and the same place the recovery sweep already looks.

    Written unconditionally: the transition on the next line takes the story out
    of the only status the poller scans, so no tick reaches this twice for the
    same ending, and a story that comes back from human review and ends this way
    again is owed the message again.
    """
    record = new_story_owner_notification(
        story_id,
        event=event,
        text=text,
        project_id=project_id,
        terminal_status=terminal_status,
    )
    await _write_story_record(api_client, story_id, record)
    log.info(
        "owner_notification_owed",
        po_event=event,
        story_id=story_id,
        project_id=project_id,
        terminal_status=terminal_status.value,
        notification_source="story",
    )
    return record


async def _write(
    api_client: SchedulerAPIClient,
    source_id: str,
    record: OwnerNotification,
    *,
    story_record: bool,
) -> None:
    """Write what one granted attempt settled, carrying that attempt's stamp."""
    try:
        if story_record:
            await _write_story_record(api_client, source_id, record)
        else:
            await _write_record(api_client, source_id, record)
    except httpx.HTTPStatusError as exc:
        if _is_superseded(exc.response):
            raise _AttemptSuperseded from exc
        raise


def _is_superseded(response: httpx.Response) -> bool:
    if response.status_code != httpx.codes.CONFLICT:
        return False
    try:
        detail = response.json().get("detail")
    except ValueError:
        return False
    return isinstance(detail, dict) and detail.get("code") == OWNER_NOTIFICATION_ATTEMPT_SUPERSEDED


async def _claim_attempt(
    api_client: SchedulerAPIClient, source_id: str, *, story_record: bool
) -> OwnerNotificationAttemptClaim:
    if story_record:
        return await api_client.claim_story_owner_notification_attempt(source_id)
    return await api_client.claim_run_owner_notification_attempt(source_id)


async def _settle(
    api_client: SchedulerAPIClient,
    source_id: str,
    record: OwnerNotification,
    *,
    state: OwnerNotificationState,
    detail: str | None = None,
    attempts: int | None = None,
    story_record: bool = False,
    delivered_at: datetime | None = None,
) -> OwnerNotification:
    update = {
        "state": state,
        "detail": detail,
        "attempts": record.attempts if attempts is None else attempts,
    }
    if delivered_at is not None:
        update["delivered_at"] = delivered_at
    settled = record.model_copy(update=update)
    await _write(api_client, source_id, settled, story_record=story_record)
    return settled


async def _abandon(
    api_client: SchedulerAPIClient,
    source_id: str,
    record: OwnerNotification,
    *,
    attempts: int,
    error: str,
    log: structlog.stdlib.BoundLogger,
    story_record: bool = False,
) -> OwnerNotification:
    """Give up on a message the owner will never receive, loudly."""
    settled = await _settle(
        api_client,
        source_id,
        record,
        state=OwnerNotificationState.ABANDONED,
        detail=error,
        attempts=attempts,
        story_record=story_record,
    )
    source_fields = _source_log_fields(source_id, story_record=story_record)
    log.error(
        "owner_notification_abandoned",
        po_event=record.event,
        story_id=record.story_id,
        project_id=record.project_id,
        attempts=attempts,
        max_attempts=OWNER_NOTIFICATION_MAX_ATTEMPTS,
        error=error,
        **source_fields,
    )
    source_description = f"source=story:{source_id}" if story_record else f"run={source_id}"
    await notify_admins_best_effort(
        f"Owner notification undelivered after {attempts} attempts: "
        f"event={record.event} story={record.story_id} "
        f"project={record.project_id} {source_description}: {error}",
        level="error",
        po_event=record.event,
        story_id=record.story_id,
        project_id=record.project_id,
        **source_fields,
    )
    return settled


async def _spend_failed_attempt(
    api_client: SchedulerAPIClient,
    source_id: str,
    record: OwnerNotification,
    *,
    attempts: int,
    error: str,
    log: structlog.stdlib.BoundLogger,
    story_record: bool = False,
) -> tuple[OwnerNotificationOutcome, OwnerNotification]:
    """Charge one transient failure to the bound, or give up if it was the last."""
    if attempts >= OWNER_NOTIFICATION_MAX_ATTEMPTS:
        settled = await _abandon(
            api_client,
            source_id,
            record,
            attempts=attempts,
            error=error,
            log=log,
            story_record=story_record,
        )
        return OwnerNotificationOutcome.EXHAUSTED, settled
    settled = await _settle(
        api_client,
        source_id,
        record,
        state=OwnerNotificationState.OWED,
        detail=error,
        attempts=attempts,
        story_record=story_record,
    )
    log.warning(
        "owner_notification_publish_failed",
        po_event=record.event,
        story_id=record.story_id,
        project_id=record.project_id,
        attempts=attempts,
        max_attempts=OWNER_NOTIFICATION_MAX_ATTEMPTS,
        error=error,
        **_source_log_fields(source_id, story_record=story_record),
    )
    return OwnerNotificationOutcome.RETRYING, settled


async def deliver_owed_notification(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    source_id: str,
    record: OwnerNotification,
    log: structlog.stdlib.BoundLogger,
    *,
    story_record: bool = False,
) -> OwnerNotificationOutcome:
    """Spend one attempt on each audience this record still owes, if one is due.

    Nothing is read, resolved or published before the API grants the attempt.
    The grant is the single place the spacing is decided — owed, and last
    attempted at least ``OWNER_NOTIFICATION_ATTEMPT_INTERVAL`` ago — and it is
    stamped in the same write, so whichever caller asks first, routing or the
    sweep, is the only one that attempts. A refusal reports ``SKIPPED`` when the
    record turned out settled and ``NOT_DUE`` when it was attempted too recently.
    The visit then works from the record the grant returned, not from the copy
    the caller read, which may already be stale.

    The owner and administrators are separate audiences of one ending, each with
    its own persisted state and bounded attempts, so a settled audience is never
    published again while the other is still retried. One grant serves both, so
    no two callers can split them. The owner is served first and the record
    written back after each audience, so a process that stops between the two
    resumes with exactly the audience that is still owed.

    The returned outcome is the owner's when the owner was owed, and otherwise
    the administrators'.
    """
    if not record.owed and not record.admin_owed:
        return OwnerNotificationOutcome.SKIPPED
    claim = await _claim_attempt(api_client, source_id, story_record=story_record)
    if not claim.granted:
        current = claim.notification
        if current is None or not (current.owed or current.admin_owed):
            return OwnerNotificationOutcome.SKIPPED
        log.info(
            "owner_notification_attempt_not_due",
            po_event=current.event,
            last_attempt_at=current.last_attempt_at.isoformat(),
            interval_seconds=OWNER_NOTIFICATION_ATTEMPT_INTERVAL.total_seconds(),
            **_source_log_fields(source_id, story_record=story_record),
        )
        return OwnerNotificationOutcome.NOT_DUE
    record = claim.notification
    outcome = OwnerNotificationOutcome.SKIPPED
    try:
        if record.owed:
            outcome, record = await _deliver_to_owner(
                api_client, redis_client, source_id, record, log, story_record=story_record
            )
        if record.admin_owed:
            admin_outcome, record = await _deliver_to_administrators(
                api_client, source_id, record, log, story_record=story_record
            )
            if outcome is OwnerNotificationOutcome.SKIPPED:
                outcome = admin_outcome
    except _AttemptSuperseded:
        log.warning(
            "owner_notification_attempt_superseded",
            po_event=record.event,
            last_attempt_at=record.last_attempt_at.isoformat(),
            **_source_log_fields(source_id, story_record=story_record),
        )
        return OwnerNotificationOutcome.SUPERSEDED
    return outcome


async def deliver_in_tick(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    run_id: str,
    record: OwnerNotification,
    log: structlog.stdlib.BoundLogger,
) -> OwnerNotificationOutcome | None:
    """Spend the routing tick's attempt on a record its move just committed.

    The move is already durable with its record, so this attempt is an
    optimisation, not the delivery guarantee: whatever it misses — a failed
    publish, a refused claim, the API unreachable for the claim itself — is
    still owed and ``supervise_owed_owner_notifications`` recovers it. An
    exception is therefore contained here, and ``None`` says no outcome was
    recorded, rather than ending the tick for unrelated rows.
    """
    try:
        return await deliver_owed_notification(api_client, redis_client, run_id, record, log)
    except Exception:
        log.warning("owner_notification_in_tick_attempt_failed", run_id=run_id, exc_info=True)
        return None


async def _spend_failed_admin_attempt(
    api_client: SchedulerAPIClient,
    source_id: str,
    record: OwnerNotification,
    *,
    attempts: int,
    error: str,
    log: structlog.stdlib.BoundLogger,
    story_record: bool,
) -> tuple[OwnerNotificationOutcome, OwnerNotification]:
    """Charge one failed administrator publication to the bound, or abandon it."""
    exhausted = attempts >= OWNER_NOTIFICATION_MAX_ATTEMPTS
    settled = record.model_copy(
        update={
            "admin_state": (
                OwnerNotificationState.ABANDONED if exhausted else OwnerNotificationState.OWED
            ),
            "admin_attempts": attempts,
            "admin_detail": error,
        }
    )
    await _write(api_client, source_id, settled, story_record=story_record)
    (log.error if exhausted else log.warning)(
        "admin_notification_abandoned" if exhausted else "admin_notification_publish_failed",
        po_event=record.event,
        story_id=record.story_id,
        project_id=record.project_id,
        attempts=attempts,
        max_attempts=OWNER_NOTIFICATION_MAX_ATTEMPTS,
        error=error,
        **_source_log_fields(source_id, story_record=story_record),
    )
    outcome = OwnerNotificationOutcome.EXHAUSTED if exhausted else OwnerNotificationOutcome.RETRYING
    return outcome, settled


async def _deliver_to_administrators(
    api_client: SchedulerAPIClient,
    source_id: str,
    record: OwnerNotification,
    log: structlog.stdlib.BoundLogger,
    *,
    story_record: bool,
) -> tuple[OwnerNotificationOutcome, OwnerNotification]:
    """Spend one bounded attempt on the administrator audience and record it.

    Unlike the owner's message, the administrators' notice describes an event
    that already committed with the record, so it is not voided by a story that
    has since moved on (an operator may recover the park before it is read).

    The settlement is read from the per-recipient result, never from the call
    not raising, because Telegram failures come back as ``False``:

    * no configured administrator — ``unaddressable``, settled with evidence;
    * every configured administrator reached — ``delivered``;
    * zero or partial success, or a raised users-API failure — a spent attempt
      that stays ``owed``, and ``abandoned`` with the detail after the bound.

    A settled audience is never sent again. Before settlement delivery is
    at-least-once: Telegram has no idempotency key, so a retry after a partial
    success, or after a crash before the record was written, resends to
    administrators who already received it.
    """
    attempts = record.admin_attempts + 1
    source_fields = _source_log_fields(source_id, story_record=story_record)
    try:
        result = await deliver_to_admins(record.admin_text, level="error")
    except Exception as exc:
        return await _spend_failed_admin_attempt(
            api_client,
            source_id,
            record,
            attempts=attempts,
            error=f"{type(exc).__name__}: {exc}",
            log=log,
            story_record=story_record,
        )
    if result.status is AdminDeliveryStatus.UNADDRESSABLE:
        settled = record.model_copy(
            update={
                "admin_state": OwnerNotificationState.UNADDRESSABLE,
                "admin_attempts": attempts,
                "admin_detail": result.detail,
            }
        )
        await _write(api_client, source_id, settled, story_record=story_record)
        log.warning(
            "admin_notification_unaddressable",
            po_event=record.event,
            story_id=record.story_id,
            project_id=record.project_id,
            detail=result.detail,
            **source_fields,
        )
        return OwnerNotificationOutcome.UNADDRESSABLE, settled
    if result.status is not AdminDeliveryStatus.DELIVERED:
        return await _spend_failed_admin_attempt(
            api_client,
            source_id,
            record,
            attempts=attempts,
            error=result.detail,
            log=log,
            story_record=story_record,
        )
    settled = record.model_copy(
        update={
            "admin_state": OwnerNotificationState.DELIVERED,
            "admin_attempts": attempts,
            "admin_detail": None,
        }
    )
    await _write(api_client, source_id, settled, story_record=story_record)
    log.info(
        "admin_notification_delivered",
        po_event=record.event,
        story_id=record.story_id,
        project_id=record.project_id,
        attempts=attempts,
        **source_fields,
    )
    return OwnerNotificationOutcome.DELIVERED, settled


@dataclass(frozen=True)
class _Truth:
    """What the story, and the task a task-level record names, say right now."""

    story_status: StoryStatus
    task_status: TaskStatus | None
    #: Why the message is not true now, or None when it is.
    untrue: str | None


async def _read_truth(api_client: SchedulerAPIClient, record: OwnerNotification) -> _Truth:
    """Read whether the state this record announces is still the state that holds.

    A read that fails raises: it is a transient failure to be charged to the
    bound, never proof that the state is gone.
    """
    story_status = (await api_client.get_story(record.story_id)).status
    if story_status is not record.terminal_status:
        return _Truth(
            story_status,
            None,
            f"story is {story_status.value}, not {record.terminal_status.value}",
        )
    if record.expected_task_statuses is None:
        return _Truth(story_status, None, None)
    # A task-level notice is true only while the task is where it was announced
    # to be; the story can stay put while the task moves on.
    task_status = (await api_client.get_task(record.task_id)).status
    if task_status in record.expected_task_statuses:
        return _Truth(story_status, task_status, None)
    expected = ", ".join(status.value for status in record.expected_task_statuses)
    return _Truth(story_status, task_status, f"task is {task_status.value}, not {expected}")


async def _void(
    api_client: SchedulerAPIClient,
    source_id: str,
    record: OwnerNotification,
    truth: _Truth,
    log: structlog.stdlib.BoundLogger,
    *,
    story_record: bool,
) -> tuple[OwnerNotificationOutcome, OwnerNotification]:
    """Settle a record whose message is not true, publishing nothing and spending nothing."""
    settled = await _settle(
        api_client,
        source_id,
        record,
        state=OwnerNotificationState.VOIDED,
        detail=truth.untrue,
        story_record=story_record,
    )
    log.warning(
        "owner_notification_voided",
        po_event=record.event,
        story_id=record.story_id,
        project_id=record.project_id,
        story_status=truth.story_status.value,
        terminal_status=record.terminal_status.value,
        task_status=None if truth.task_status is None else truth.task_status.value,
        **_source_log_fields(source_id, story_record=story_record),
    )
    return OwnerNotificationOutcome.VOIDED, settled


async def _deliver_to_owner(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    source_id: str,
    record: OwnerNotification,
    log: structlog.stdlib.BoundLogger,
    *,
    story_record: bool = False,
) -> tuple[OwnerNotificationOutcome, OwnerNotification]:
    """Spend one attempt on the owner audience and record what happened.

    Nothing is published before the story is read and found in the
    ``terminal_status`` this record was written for. The record was written
    first, so on its own it says only what the supervisor *intended*; the story
    is what says the intention was committed. A story that is not there yet is
    not a failure to retry — no message is due, so the record is voided and no
    attempt is spent, and the ending is owed again if routing reaches it later.
    Reading the story is itself an API call, so a lookup that failed is treated
    as the transient failure it is, not as proof of a missing transition.

    A record that names ``expected_task_statuses`` — a task's resource wait or
    its resumption — is checked against its task as well, the same way: a task
    that has moved out of those statuses voids it, and a task read that failed
    spends an attempt. A record without them keeps exactly the story check.

    The check is made twice. First, before the recipient is resolved, so a
    record whose state never held is voided without a recipient lookup (and
    without the administrator alert an unresolvable owner raises). Then again
    as the last reads before the publish: resolving the recipient is API calls
    that can stall, and a state that changed meanwhile — a resume committing
    while a "waiting" notice was being addressed — must not be announced as it
    was. Nothing slow sits between that final check and the ``XADD``.

    A recipient lookup that fails is a spent attempt like a stream that refused
    the write: neither delivered anything. What is *not* the same is a recipient
    that resolved to nothing; that is an answer, not a failure, and repeating
    the question would only produce it again.
    """
    attempts = record.attempts + 1

    async def spend(exc: Exception) -> tuple[OwnerNotificationOutcome, OwnerNotification]:
        return await _spend_failed_attempt(
            api_client,
            source_id,
            record,
            attempts=attempts,
            error=f"{type(exc).__name__}: {exc}",
            log=log,
            story_record=story_record,
        )

    try:
        truth = await _read_truth(api_client, record)
    except Exception as exc:
        return await spend(exc)
    if truth.untrue is not None:
        return await _void(api_client, source_id, record, truth, log, story_record=story_record)

    try:
        recipient = await resolve_project_recipient(
            api_client, record.project_id, event=record.event, story_id=record.story_id
        )
    except Exception as exc:
        return await spend(exc)
    if not recipient.is_addressable:
        settled = await _settle(
            api_client,
            source_id,
            record,
            state=OwnerNotificationState.UNADDRESSABLE,
            detail=recipient.unaddressed_reason,
            attempts=attempts,
            story_record=story_record,
        )
        log.warning(
            "owner_notification_unaddressable",
            po_event=record.event,
            story_id=record.story_id,
            project_id=record.project_id,
            reason=recipient.unaddressed_reason,
            **_source_log_fields(source_id, story_record=story_record),
        )
        return OwnerNotificationOutcome.UNADDRESSABLE, settled

    # The last reads before the publish: whatever changed while the recipient
    # was being resolved is seen here, not after the message is out.
    try:
        truth = await _read_truth(api_client, record)
    except Exception as exc:
        return await spend(exc)
    if truth.untrue is not None:
        return await _void(api_client, source_id, record, truth, log, story_record=story_record)

    try:
        event = POSystemEvent(
            event=record.event,
            # PO answers about the subject the record names: the task for a
            # task-level ending, the story for a story-level one.
            task_id=record.story_id if record.task_id is None else record.task_id,
            text=record.text,
            story_id=record.story_id,
            telegram_chat_id=recipient.telegram_chat_id,
            owner_user_id=recipient.owner_user_id,
            project_id=record.project_id,
        )
        await redis_client.publish_flat(PO_INPUT_QUEUE, to_flat_fields(event))
    except Exception as exc:
        return await spend(exc)

    settled = await _settle(
        api_client,
        source_id,
        record,
        state=OwnerNotificationState.DELIVERED,
        attempts=attempts,
        story_record=story_record,
        delivered_at=datetime.now(UTC),
    )
    log.info(
        "owner_notification_delivered",
        po_event=record.event,
        story_id=record.story_id,
        project_id=record.project_id,
        attempts=attempts,
        **_source_log_fields(source_id, story_record=story_record),
    )
    return OwnerNotificationOutcome.DELIVERED, settled


async def supervise_owed_owner_notifications(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """Re-attempt every message a committed terminal transition still owes.

    This is the recovery entry point the supervisor's own loops cannot be: they
    scan stories by status, and a terminal transition is precisely what takes a
    story out of the status that would bring it back. The selection is the state
    of the record instead, so a story finished during an outage is still served
    when the process comes back. What the selection deliberately does *not* do
    is decide anything: whether the transition the record was written for is
    really there is settled per record, against the story, inside
    ``deliver_owed_notification``.

    It runs on its own scheduler loop, apart from the dispatcher tick whose
    routing makes the in-tick attempts, and neither the loop's cadence nor its
    timing against routing spaces the attempts. The spacing is the record's
    ``last_attempt_at`` claim at the API: each visit asks for the attempt, and
    the API grants one per record per ``OWNER_NOTIFICATION_ATTEMPT_INTERVAL``,
    stamped on the record under its row lock. A record routing has just
    attempted is refused here as ``not_due``, and a record this sweep has just
    attempted is refused to routing, in either order and when both ask at once.
    """
    counts = _empty_counts()
    runs = await api_client.list_runs_owing_owner_notification(limit=OWNER_NOTIFICATION_PAGE)
    for run in runs:
        record = read_owner_notification(run)
        if record is None:
            raise RuntimeError(
                f"Run {run.id} was selected as owing a notification but carries none"
            )
        log = logger.bind(story_id=record.story_id, project_id=record.project_id)
        outcome = await deliver_owed_notification(api_client, redis_client, run.id, record, log)
        counts[outcome.value] += 1
    stories = await api_client.list_stories_owing_owner_notification(limit=OWNER_NOTIFICATION_PAGE)
    for story in stories:
        record = read_story_owner_notification(story)
        if record is None:
            raise RuntimeError(
                f"Story {story.id} was selected as owing a notification but carries none"
            )
        log = logger.bind(story_id=record.story_id, project_id=record.project_id)
        outcome = await deliver_owed_notification(
            api_client, redis_client, story.id, record, log, story_record=True
        )
        counts[outcome.value] += 1
    return counts

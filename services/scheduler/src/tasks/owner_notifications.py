"""The one seam every terminal owner notification from the supervisor goes through.

The invariant it holds: a terminal story transition cannot be observed without
the owner's message being either already published to ``po:input`` or durably
owed. Nothing here is best-effort. ``owe_owner_notification`` writes the message
down on the QA run *before* the transition is committed, and from that moment the
record is the work item — ``deliver_owed_notification`` is the only thing that
settles it, whether it is called by the tick that owed the message or by the
recovery sweep two ticks later.

Why the record and not just a careful publish: the publish is an ``xadd`` with
nothing behind it, and the transition in front of it is a commit. Publish after
commit and a transient failure loses the message forever, because the story has
left the status the supervisor scans and no later tick sees it. Publish before
commit and a failure to commit leaves a user told about an outcome that did not
happen. The record removes the choice — it is written first, the transition is
committed, and the delivery is retried from the record until it lands or a human
is called.

Three endings, and they are deliberately not interchangeable:

* delivered — ``po:input`` accepted the event; nothing publishes it again.
* unaddressable — the owner resolved to no Telegram chat. Retrying that changes
  nothing, so it is a logged, alerted refusal and the record is settled.
* abandoned — transient failures used up the bounded attempts. An administrator
  is told, with the story, project, event and run id.

Delivery is at-least-once, not exactly-once. A process that dies between the
publish landing and the record being marked delivered republishes on the next
tick. That is the honest trade for never losing the message; what the record
does guarantee is that a *settled* notification is never published again.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from shared.contracts.dto.owner_notification import (
    OWNER_NOTIFICATION_KEY,
    OwnerNotification,
    OwnerNotificationState,
)
from shared.contracts.queues.po import POSystemEvent, to_flat_fields
from shared.notifications import notify_admins_best_effort
from shared.queues import PO_INPUT_QUEUE
from shared.redis_client import RedisStreamClient

from ._recipients import resolve_project_recipient

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient

logger = structlog.get_logger(__name__)

#: Publishes one message may cost before the owner is declared undeliverable and
#: an administrator is called. Counted on the record, so a process that dies
#: mid-delivery cannot restart the count — the same reason the transport leg
#: counts its deliveries on Redis' PEL rather than in memory.
OWNER_NOTIFICATION_MAX_ATTEMPTS = 3

#: Runs the recovery sweep takes per tick. The selection drains by itself, since
#: every visit either delivers a record or spends one of its bounded attempts.
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
    #: The record was already settled by somebody else. Nothing was published.
    SKIPPED = "skipped"


def _empty_counts() -> dict[str, int]:
    return {outcome.value: 0 for outcome in OwnerNotificationOutcome}


async def _write_record(
    api_client: SchedulerAPIClient, run_id: str, record: OwnerNotification
) -> None:
    """Put the record on the run. ``run_metadata`` is merged by the API."""
    await api_client.update_run(
        run_id, {"run_metadata": {OWNER_NOTIFICATION_KEY: record.model_dump(mode="json")}}
    )


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


async def owe_owner_notification(
    api_client: SchedulerAPIClient,
    run,
    *,
    event: str,
    text: str,
    story_id: str,
    project_id: str,
    log: structlog.stdlib.BoundLogger,
) -> OwnerNotification:
    """Write down that the owner is owed this message. Call before the transition.

    Returns the record that now owns the delivery, which is the existing one
    when there already is one: a tick repeating a transition it already made
    must not reset a delivery in flight, and must not owe a second copy of a
    message that has already been delivered.
    """
    existing = read_owner_notification(run)
    if existing is not None:
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
        state=OwnerNotificationState.OWED,
        owed_at=datetime.now(UTC),
    )
    await _write_record(api_client, run.id, record)
    log.info("owner_notification_owed", run_id=run.id, po_event=event, story_id=story_id)
    return record


async def _settle(
    api_client: SchedulerAPIClient,
    run_id: str,
    record: OwnerNotification,
    *,
    state: OwnerNotificationState,
    detail: str | None = None,
    attempts: int | None = None,
) -> None:
    await _write_record(
        api_client,
        run_id,
        record.model_copy(
            update={
                "state": state,
                "detail": detail,
                "attempts": record.attempts if attempts is None else attempts,
            }
        ),
    )


async def _abandon(
    api_client: SchedulerAPIClient,
    run_id: str,
    record: OwnerNotification,
    *,
    attempts: int,
    error: str,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Give up on a message the owner will never receive, loudly."""
    await _settle(
        api_client,
        run_id,
        record,
        state=OwnerNotificationState.ABANDONED,
        detail=error,
        attempts=attempts,
    )
    log.error(
        "owner_notification_abandoned",
        run_id=run_id,
        po_event=record.event,
        story_id=record.story_id,
        project_id=record.project_id,
        attempts=attempts,
        max_attempts=OWNER_NOTIFICATION_MAX_ATTEMPTS,
        error=error,
    )
    await notify_admins_best_effort(
        f"Owner notification undelivered after {attempts} attempts: "
        f"event={record.event} story={record.story_id} "
        f"project={record.project_id} run={run_id}: {error}",
        level="error",
        po_event=record.event,
        story_id=record.story_id,
        project_id=record.project_id,
        run_id=run_id,
    )


async def deliver_owed_notification(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    run_id: str,
    record: OwnerNotification,
    log: structlog.stdlib.BoundLogger,
) -> OwnerNotificationOutcome:
    """Spend one attempt on an owed message and record what happened.

    Resolving the recipient and publishing are one attempt on purpose: both sit
    between the committed transition and the owner, and both fail the same way —
    a lookup that timed out is no more delivered than a stream that refused the
    write. What is *not* the same is a recipient that resolved to nothing; that
    is an answer, not a failure, and repeating the question would only produce
    it again.
    """
    if not record.owed:
        return OwnerNotificationOutcome.SKIPPED

    attempts = record.attempts + 1
    try:
        recipient = await resolve_project_recipient(
            api_client, record.project_id, event=record.event, story_id=record.story_id
        )
        if not recipient.is_addressable:
            await _settle(
                api_client,
                run_id,
                record,
                state=OwnerNotificationState.UNADDRESSABLE,
                detail=recipient.unaddressed_reason,
                attempts=attempts,
            )
            log.warning(
                "owner_notification_unaddressable",
                run_id=run_id,
                po_event=record.event,
                story_id=record.story_id,
                project_id=record.project_id,
                reason=recipient.unaddressed_reason,
            )
            return OwnerNotificationOutcome.UNADDRESSABLE

        event = POSystemEvent(
            event=record.event,
            text=record.text,
            task_id=record.story_id,
            story_id=record.story_id,
            telegram_chat_id=recipient.telegram_chat_id,
            owner_user_id=recipient.owner_user_id,
            project_id=record.project_id,
        )
        await redis_client.publish_flat(PO_INPUT_QUEUE, to_flat_fields(event))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if attempts >= OWNER_NOTIFICATION_MAX_ATTEMPTS:
            await _abandon(api_client, run_id, record, attempts=attempts, error=error, log=log)
            return OwnerNotificationOutcome.EXHAUSTED
        await _settle(
            api_client,
            run_id,
            record,
            state=OwnerNotificationState.OWED,
            detail=error,
            attempts=attempts,
        )
        log.warning(
            "owner_notification_publish_failed",
            run_id=run_id,
            po_event=record.event,
            story_id=record.story_id,
            project_id=record.project_id,
            attempts=attempts,
            max_attempts=OWNER_NOTIFICATION_MAX_ATTEMPTS,
            error=error,
        )
        return OwnerNotificationOutcome.RETRYING

    await _settle(
        api_client, run_id, record, state=OwnerNotificationState.DELIVERED, attempts=attempts
    )
    log.info(
        "owner_notification_delivered",
        run_id=run_id,
        po_event=record.event,
        story_id=record.story_id,
        project_id=record.project_id,
        attempts=attempts,
    )
    return OwnerNotificationOutcome.DELIVERED


async def supervise_owed_owner_notifications(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """Re-attempt every message a committed terminal transition still owes.

    This is the recovery entry point the supervisor's own loops cannot be: they
    scan stories by status, and a terminal transition is precisely what takes a
    story out of the status that would bring it back. The selection is the state
    of the record instead, so a story finished during an outage is still served
    when the process comes back.

    It runs before the routing step of the tick, not after, so a record owed by
    this tick's routing gets exactly the one in-tick attempt that routing makes
    and is not attempted twice in the same cycle.
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
    return counts

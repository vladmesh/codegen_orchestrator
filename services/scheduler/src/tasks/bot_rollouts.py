"""Reconciliation for configuration-only bot-audience rollouts.

A rollout is staged as a deploy run plus a publish-intent record committed
*before* anything is published. The API settles that record to published right
after its own queue write lands, so the normal path never reaches this sweep
for publishing at all. What remains here is recovery: if the process died (or
Redis failed) between commit and publish, the run sits QUEUED with its record
honestly saying the publish is owed, and this sweep puts the message on
`deploy:queue` until the stream accepts or attempts run out.

The sweep also delivers the rollout's terminal outcome to an owner who was
promised it: a run carrying an owed notify record and a terminal deploy state
becomes one proactive message, and the record is flipped to delivered. The
PO tool that observed the verdict inside the conversation window marks the
record delivered itself, so the two paths cannot double-send.

Idempotency comes from the records, not from luck. A republish after a lost ack
is absorbed by the deploy consumer's own guards (project deploy lock,
redundant-deploy shortcut); a notify is delivered exactly once because only the
transition to delivered stops the sweep.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from shared.contracts.bot_rollout import (
    BOT_ROLLOUT_METADATA_KEY,
    BOT_ROLLOUT_NOTIFY_KEY,
    BotRolloutNotifyRecord,
    BotRolloutNotifyState,
    BotRolloutPublishState,
    BotRolloutRecord,
    BotRolloutStatus,
    rollout_status_for_run,
)
from shared.contracts.queues.deploy import DeployAction, DeployMessage, DeployTrigger
from shared.contracts.queues.po import POProactiveMessage, to_flat_fields
from shared.notifications import notify_admins_best_effort
from shared.queues import DEPLOY_QUEUE, PO_PROACTIVE_QUEUE
from shared.redis_client import RedisStreamClient

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient


logger = structlog.get_logger(__name__)

#: Runs the sweep takes per tick. A rollout is one run per audience change, so
#: a page bounds the work per tick without a cursor: every visit either settles
#: its record or spends one of its bounded attempts.
BOT_ROLLOUT_PAGE = 100

#: Publish attempts one rollout may spend before a human is called. Mirrors the
#: contract constant; kept local so the sweep's bound is visible in its module.
MAX_PUBLISH_ATTEMPTS = 3


class RolloutSweepOutcome(StrEnum):
    """What this tick did to one rollout run."""

    PUBLISHED = "published"
    PUBLISH_RETRYING = "publish_retrying"
    PUBLISH_EXHAUSTED = "publish_exhausted"
    NOTIFIED = "notified"
    STILL_RUNNING = "still_running"
    SKIPPED = "skipped"


def _empty_counts() -> dict[str, int]:
    return {outcome.value: 0 for outcome in RolloutSweepOutcome}


async def reconcile_bot_rollouts(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """One sweep pass over every rollout whose bookkeeping is not settled.

    Selected by the state of the record — a run stranded by a crash hours ago
    is exactly as visible as one from a minute ago — and bounded by the page.
    """
    counts = _empty_counts()
    runs = await api_client.list_bot_rollout_runs(limit=BOT_ROLLOUT_PAGE)
    for run in runs:
        stored = (run.run_metadata or {}).get(BOT_ROLLOUT_METADATA_KEY)
        if stored is None:
            # Selected by the run id prefix, so this is a malformed record:
            # fail loudly rather than silently skip a rollout nobody owns.
            logger.error("bot_rollout_record_missing", run_id=run.id)
            counts[RolloutSweepOutcome.SKIPPED.value] += 1
            continue
        record = BotRolloutRecord.model_validate(stored)
        log = logger.bind(run_id=run.id, project_id=run.project_id)

        if record.publish_owed:
            outcome = await _settle_publish(api_client, redis_client, run, record, log)
            counts[outcome.value] += 1
            if outcome in {
                RolloutSweepOutcome.PUBLISH_RETRYING,
                RolloutSweepOutcome.PUBLISH_EXHAUSTED,
            }:
                # The message is still not on the queue: nothing else to do
                # for this run this tick.
                continue

        notify_stored = (run.run_metadata or {}).get(BOT_ROLLOUT_NOTIFY_KEY)
        if notify_stored is None:
            continue
        notify_record = BotRolloutNotifyRecord.model_validate(notify_stored)
        if not notify_record.owed:
            continue
        outcome = await _deliver_terminal_notification(
            api_client, redis_client, run, notify_record, log
        )
        counts[outcome.value] += 1

    return counts


async def _settle_publish(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    run,
    record: BotRolloutRecord,
    log: structlog.stdlib.BoundLogger,
) -> RolloutSweepOutcome:
    """Put the staged message on the deploy queue, or charge the attempt.

    The message is rebuilt deterministically from the record — the same run id
    (which is the deploy's task id), project and recorded SHA — so a republish
    is equivalent to the one that was lost, not byte-identical to it. A
    duplicate delivery is absorbed by the deploy consumer's own guards: its
    project deploy lock and its redundant-deploy shortcut.
    """
    attempts = record.attempts + 1
    try:
        await redis_client.publish_message(
            DEPLOY_QUEUE,
            DeployMessage(
                task_id=run.id,
                project_id=run.project_id,
                telegram_chat_id="",
                unaddressed_reason=(
                    "configuration-only bot-audience rollout, reconciled by the scheduler"
                ),
                story_id="",
                triggered_by=DeployTrigger.PO,
                action=DeployAction.FEATURE,
                head_sha=record.head_sha,
                env_overrides={},
            ),
        )
    except Exception as exc:
        exhausted = attempts >= MAX_PUBLISH_ATTEMPTS
        updated = record.model_copy(
            update={
                "attempts": attempts,
                "detail": f"{type(exc).__name__}: {exc}",
                "publish": (
                    BotRolloutPublishState.ABANDONED
                    if exhausted
                    else BotRolloutPublishState.PUBLISH_OWED
                ),
            }
        )
        await api_client.update_run(
            run.id,
            {"run_metadata": {BOT_ROLLOUT_METADATA_KEY: updated.model_dump(mode="json")}},
        )
        if exhausted:
            log.error("bot_rollout_publish_abandoned", attempts=attempts)
            await notify_admins_best_effort(
                f"Bot-audience rollout {run.id} (project {run.project_id}) could not be "
                f"published after {attempts} attempts: {exc}",
                level="error",
                run_id=run.id,
                project_id=run.project_id,
            )
            return RolloutSweepOutcome.PUBLISH_EXHAUSTED
        log.warning("bot_rollout_publish_retrying", attempts=attempts, error=str(exc))
        return RolloutSweepOutcome.PUBLISH_RETRYING

    updated = record.model_copy(update={"publish": BotRolloutPublishState.PUBLISHED})
    await api_client.update_run(
        run.id, {"run_metadata": {BOT_ROLLOUT_METADATA_KEY: updated.model_dump(mode="json")}}
    )
    log.info("bot_rollout_published", attempts=attempts, head_sha=record.head_sha)
    return RolloutSweepOutcome.PUBLISHED


async def _deliver_terminal_notification(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    run,
    notify_record: BotRolloutNotifyRecord,
    log: structlog.stdlib.BoundLogger,
) -> RolloutSweepOutcome:
    """Deliver one promised rollout ending, or leave the record owed.

    The ending is only real once the deploy run says so: a rollout still
    running keeps its promise unspent. A cancelled run reads as pending here
    (see `rollout_status_for_run`) and keeps waiting too — the sweep's own
    publish recovery or a successor decides the ending.
    """
    status_value = run.status.value if hasattr(run.status, "value") else run.status
    status, detail = rollout_status_for_run(
        run_status=status_value,
        result=run.result,
        error_message=run.error_message,
    )
    if status is not BotRolloutStatus.APPLIED and status is not BotRolloutStatus.FAILED:
        return RolloutSweepOutcome.STILL_RUNNING

    if status is BotRolloutStatus.APPLIED:
        text = "Your bot access change is now live — the running bot has the new audience."
    else:
        text = (
            "Your bot access change did NOT reach the running bot "
            f"({detail or 'rollout failed'}). The bot is still running with the "
            "previous audience."
        )

    try:
        await redis_client.publish_flat(
            PO_PROACTIVE_QUEUE,
            to_flat_fields(
                POProactiveMessage(
                    text=text,
                    telegram_chat_id=notify_record.telegram_chat_id,
                    event="bot_audience_rollout_terminal",
                    project_id=run.project_id,
                )
            ),
        )
    except Exception as exc:
        log.warning("bot_rollout_notify_retrying", error=str(exc))
        return RolloutSweepOutcome.STILL_RUNNING

    delivered = notify_record.model_copy(update={"state": BotRolloutNotifyState.DELIVERED})
    await api_client.update_run(
        run.id, {"run_metadata": {BOT_ROLLOUT_NOTIFY_KEY: delivered.model_dump(mode="json")}}
    )
    log.info("bot_rollout_notified", rollout_status=status.value)
    return RolloutSweepOutcome.NOTIFIED

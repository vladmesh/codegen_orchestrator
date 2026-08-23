"""Shared orchestration for bot-audience mutations and their rollouts.

One function every audience endpoint goes through, so authorization, row-lock
atomicity, idempotency, the final-ID guard, the publish-intent record and the
config-only rollout staging are written once and cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
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
from shared.crypto import decrypt_dict, encrypt_dict
from shared.models import Run
from shared.queues import DEPLOY_QUEUE
from shared.redis.client import RedisStreamClient

from ..utils.bot_audience import (
    LEGACY_BOT_AUDIENCE_KEY,
    AudienceOperation,
    IdempotentOutcome,
    apply_audience_mutation,
    find_live_rollout_target,
    find_publish_owed_run,
    find_running_without_recorded_sha,
    no_private_audience_detail,
    resolve_updated_audience,
    stage_config_rollout,
    stored_audience,
    unrecorded_target_detail,
)
from ._recipients import resolve_project_recipient
from .projects_guards import check_project_access, load_locked_project

logger = structlog.get_logger()


@dataclass(frozen=True)
class StagedPublish:
    """What must be published only after the mutation transaction commits."""

    message: object  # DeployMessage; typed loosely to keep this module HTTP-free
    run_id: str


@dataclass(frozen=True)
class MutationOutcome:
    """The two effects of one mutation, reported separately on the wire."""

    mode: str
    operation_value: str
    audience: str
    rollout_status: BotRolloutStatus
    rollout_run_id: str | None = None


def _idempotent_outcome(operation: AudienceOperation) -> IdempotentOutcome:
    if operation is AudienceOperation.ADD:
        return IdempotentOutcome.ALREADY_PRESENT
    if operation is AudienceOperation.REMOVE:
        return IdempotentOutcome.ALREADY_ABSENT
    return IdempotentOutcome.ALREADY_SET


def apply_set_mutation(config: dict, *, mode: str, audience: str) -> dict:
    """Rewrite both contract locations for a whole-audience selection."""
    new_config = dict(config)
    overrides = dict(new_config.get("env_overrides") or {})
    overrides["TG_BOT_ALLOWED_TELEGRAM_IDS"] = audience
    new_config["env_overrides"] = overrides
    new_config["bot_access"] = {"mode": mode, "allowed_telegram_ids": audience}
    return new_config


def drop_legacy_secret(config: dict) -> bool:
    """Remove the encrypted legacy private marker, if present. Mutates config."""
    secrets = config.get("secrets") or {}
    if not secrets:
        return False
    existing = decrypt_dict(secrets)
    if LEGACY_BOT_AUDIENCE_KEY not in existing:
        return False
    del existing[LEGACY_BOT_AUDIENCE_KEY]
    config["secrets"] = encrypt_dict(existing)
    return True


async def mark_rollout_published(db: AsyncSession, run_id: str) -> None:
    """Settle the publish intent after the queue write landed.

    Called by every publisher right after `publish_message` returns: the record
    moves to published so neither the next mutation nor the scheduler sweep
    republishes a message that is already on the stream. A duplicate delivery
    would be absorbed by the deploy consumer's guards, but the lock-not-acquired
    path it can hit cancels the very run it races — so the honest record is what
    keeps the happy path free of that race at all.
    """
    query = select(Run).where(Run.id == run_id)
    run = (await db.execute(query)).scalar_one_or_none()
    if run is None:
        logger.error("bot_rollout_run_missing_after_publish", run_id=run_id)
        return
    stored = (run.run_metadata or {}).get(BOT_ROLLOUT_METADATA_KEY)
    if stored is None:
        logger.error("bot_rollout_record_missing_after_publish", run_id=run_id)
        return
    record = BotRolloutRecord.model_validate(stored)
    if record.publish is BotRolloutPublishState.PUBLISHED:
        return
    run.run_metadata = {
        **(run.run_metadata or {}),
        BOT_ROLLOUT_METADATA_KEY: record.model_copy(
            update={"publish": BotRolloutPublishState.PUBLISHED}
        ).model_dump(mode="json"),
    }
    await db.commit()


async def mutate_bot_audience(
    db: AsyncSession,
    project_id: uuid.UUID,
    redis: RedisStreamClient,
    *,
    x_telegram_id: int | None,
    is_internal: bool,
    operation: AudienceOperation,
    telegram_id: int | None = None,
    set_mode: str | None = None,
    set_audience: str | None = None,
) -> tuple[MutationOutcome, StagedPublish | None]:
    """Apply one audience mutation under the project row lock.

    ADD/REMOVE extend or shrink the stored private audience by exactly one ID.
    SET rewrites mode + whole audience for `set_bot_access` (which validated its
    own mode/audience pair first) and drops a legacy private secret.

    When a bot is running and something changed, this also stages the
    config-only rollout, commits it with a durable publish-intent record,
    publishes the message and settles that record to published — so a rollout
    that left this function is fully on the queue, and one that died before it
    is resumable by the scheduler sweep from the committed record.

    When nothing changes the response says so without writing or rolling out.
    Unfinished rollout work from an earlier interrupted attempt is reconciled by
    the scheduler sweep — not here — so "nothing changed" never masks a rollout
    stuck in QUEUED with its publish still owed.

    Returns the outcome plus what was published (None when there is nothing
    live to roll out).
    """
    project = await load_locked_project(db, project_id)
    await check_project_access(project, x_telegram_id, db, is_internal=is_internal)

    config = dict(project.config or {})
    access = config.get("bot_access")

    if operation is AudienceOperation.SET:
        # set_bot_access owns validation of the mode/audience pair; this path
        # carries the literal in, records the selection, and migrates away a
        # legacy private secret exactly as it always did.
        assert set_mode is not None and set_audience is not None
        new_config = apply_set_mutation(config, mode=set_mode, audience=set_audience)
        changed = new_config["bot_access"] != access
        legacy_dropped = drop_legacy_secret(new_config)
        changed = changed or legacy_dropped
        updated = set_audience
    else:
        stored = stored_audience(config)
        if stored is None:
            raise HTTPException(status_code=422, detail=no_private_audience_detail(config))
        assert telegram_id is not None
        updated = resolve_updated_audience(stored, telegram_id, operation)
        changed = updated != stored
        legacy_dropped = False
        new_config = apply_audience_mutation(config, updated=updated) if changed else config

    if not changed:
        # The stored state already matches the request: no write, no new
        # rollout. But "nothing changed" must not mask unfinished work from an
        # earlier interrupted attempt — if a previous rollout is still owed its
        # queue write, the response says so, and the scheduler sweep resumes it.
        outstanding = await find_publish_owed_run(db, project_id)
        logger.info(
            "project_bot_audience_unchanged",
            project_id=str(project_id),
            operation=operation.value,
            telegram_id=telegram_id,
            audience=updated,
            outstanding_rollout_run_id=outstanding.id if outstanding else None,
        )
        return (
            MutationOutcome(
                mode=new_config.get("bot_access", {}).get("mode", ""),
                operation_value=_idempotent_outcome(operation).value,
                audience=str(updated),
                rollout_status=(
                    BotRolloutStatus.PENDING if outstanding else BotRolloutStatus.NOT_DEPLOYED
                ),
                rollout_run_id=outstanding.id if outstanding else None,
            ),
            None,
        )

    target = await find_live_rollout_target(db, project_id)
    staged_publish: StagedPublish | None = None
    if target is None and await find_running_without_recorded_sha(db, project_id):
        # Running but unattributable to any commit: refuse loudly rather than
        # skip, so nobody believes an unreachable bot got the new audience.
        raise HTTPException(status_code=409, detail=unrecorded_target_detail())

    recipient = await resolve_project_recipient(
        db, project_id, event=f"bot_audience_{operation.value}"
    )
    if target is not None:
        staged = stage_config_rollout(
            project=project,
            target=target,
            recipient_chat_id=recipient.telegram_chat_id,
            unaddressed_reason=recipient.unaddressed_reason,
        )
        db.add(staged.run)
        staged_publish = StagedPublish(message=staged.message, run_id=staged.run.id)

    project.config = new_config
    await db.commit()

    # Publish only after the commit: the durable record on the run says the
    # publish is owed until this write lands, so a crash before here leaves
    # resumable work for the scheduler sweep rather than a lost rollout.
    if staged_publish is not None:
        await redis.publish_message(DEPLOY_QUEUE, staged_publish.message)
        await mark_rollout_published(db, staged_publish.run_id)

    if legacy_dropped:
        logger.info("legacy_bot_access_replaced", project_id=str(project_id), mode=set_mode)

    logger.info(
        "project_bot_audience_mutated",
        project_id=str(project_id),
        operation=operation.value,
        telegram_id=telegram_id,
        audience=updated,
        mode=set_mode,
        rollout="pending" if staged_publish else "not_deployed",
        rollout_run_id=staged_publish.run_id if staged_publish else None,
    )
    outcome = MutationOutcome(
        mode=new_config["bot_access"]["mode"],
        operation_value=operation.value,
        audience=str(updated),
        rollout_status=(
            BotRolloutStatus.PENDING if staged_publish else BotRolloutStatus.NOT_DEPLOYED
        ),
        rollout_run_id=staged_publish.run_id if staged_publish else None,
    )
    return outcome, staged_publish


async def rollout_status(
    db: AsyncSession,
    project_id: uuid.UUID,
    run_id: str,
    *,
    x_telegram_id: int | None,
    is_internal: bool,
) -> tuple[BotRolloutStatus, str]:
    """Where a rollout stands, after proving it belongs to this project.

    A rollout run is bound twice: `run.project_id == project_id` in SQL, and
    the canonical project access check on top. A run id from another project —
    or another owner's — is indistinguishable from a missing one.
    """
    query = select(Run).where(Run.id == run_id, Run.project_id == project_id)
    run = (await db.execute(query)).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Rollout run not found")

    # The canonical project access check, after proving the run is bound to
    # this project. A named user who is not the owner is refused here exactly
    # as they are on every other project read.
    project = await load_locked_project(db, project_id)
    await check_project_access(project, x_telegram_id, db, is_internal=is_internal)

    status_value, detail = rollout_status_for_run(
        run_status=run.status,
        result=run.result,
        error_message=run.error_message,
    )
    return status_value, detail


async def owe_rollout_notification(
    db: AsyncSession,
    project_id: uuid.UUID,
    run_id: str,
    *,
    x_telegram_id: int | None,
    is_internal: bool,
) -> dict:
    """Record that this rollout's terminal outcome is still owed to the owner.

    Called by the PO tool when its bounded wait ended with the rollout still
    pending — the reply goes out saying "in progress", and this durable marker
    is what makes the eventual ending reach the user anyway. The scheduler
    sweep selects runs carrying an owed record whose deploy has finished,
    delivers one proactive message, and flips the state to delivered; the
    endpoint itself is idempotent (a second call never resets a delivery).
    """
    from datetime import UTC, datetime

    query = select(Run).where(Run.id == run_id, Run.project_id == project_id)
    run = (await db.execute(query)).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Rollout run not found")

    project = await load_locked_project(db, project_id)
    await check_project_access(project, x_telegram_id, db, is_internal=is_internal)

    existing = (run.run_metadata or {}).get(BOT_ROLLOUT_NOTIFY_KEY)
    if existing is not None:
        record = BotRolloutNotifyRecord.model_validate(existing)
        if not record.owed:
            # Already reported by somebody. Idempotent repeat: nothing changes.
            return {"state": record.state.value}
    else:
        recipient = await resolve_project_recipient(
            db, project_id, event="bot_audience_rollout_terminal"
        )
        record = BotRolloutNotifyRecord(
            state=BotRolloutNotifyState.OWED,
            telegram_chat_id=recipient.telegram_chat_id,
            owed_at=datetime.now(UTC),
        )

    run.run_metadata = {
        **(run.run_metadata or {}),
        BOT_ROLLOUT_NOTIFY_KEY: record.model_dump(mode="json"),
    }
    await db.commit()
    logger.info("rollout_terminal_notification_owed", run_id=run_id, project_id=str(project_id))
    return {"state": record.state.value}


__all__ = [
    "MutationOutcome",
    "StagedPublish",
    "mark_rollout_published",
    "mutate_bot_audience",
    "owe_rollout_notification",
    "rollout_status",
]

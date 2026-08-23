"""The rollout reconciliation sweep: publish recovery and terminal delivery.

A rollout's publish intent is committed *before* the queue write, so a lost
publish leaves a QUEUED run whose record says the message is owed. The sweep
retries from that record until the stream accepts or attempts run out; it also
delivers the promised terminal outcome to an owner who stopped waiting. Both
settle by transition, so nothing can be double-sent or stranded forever.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.bot_rollout import (
    BOT_ROLLOUT_METADATA_KEY,
    BOT_ROLLOUT_NOTIFY_KEY,
    BotRolloutNotifyRecord,
    BotRolloutNotifyState,
    BotRolloutPublishState,
    BotRolloutRecord,
)
from shared.contracts.dto.run import RunStatus
from shared.contracts.queues.deploy import DeployAction, DeployTrigger
from src.tasks.bot_rollouts import reconcile_bot_rollouts


def _make_run(
    *,
    run_id: str = "botrollout-abc123",
    status=RunStatus.QUEUED,
    record: BotRolloutRecord | None = None,
    notify: dict | None = None,
) -> MagicMock:
    run = MagicMock()
    run.id = run_id
    run.project_id = "p-1"
    run.status = status
    run.result = None
    run.error_message = None
    metadata = {BOT_ROLLOUT_METADATA_KEY: (record or _owed_record()).model_dump(mode="json")}
    if notify is not None:
        metadata[BOT_ROLLOUT_NOTIFY_KEY] = notify
    run.run_metadata = metadata
    return run


def _owed_record(attempts: int = 0) -> BotRolloutRecord:
    return BotRolloutRecord(
        publish=BotRolloutPublishState.PUBLISH_OWED,
        application_id=7,
        head_sha="a" * 40,
        staged_at=datetime.now(UTC),
        attempts=attempts,
    )


def _clients():
    api_client = AsyncMock()
    redis_client = AsyncMock()
    redis_client.publish_message = AsyncMock(return_value="1-1")
    redis_client.publish_flat = AsyncMock(return_value="1-2")
    return api_client, redis_client


@pytest.mark.asyncio
async def test_a_owed_publish_is_retried_and_marked_published():
    """The commit/publish gap closes: the sweep puts the staged message on the
    queue and flips the record to published."""
    api_client, redis_client = _clients()
    api_client.list_bot_rollout_runs.return_value = [_make_run()]

    counts = await reconcile_bot_rollouts(api_client, redis_client)

    redis_client.publish_message.assert_awaited_once()
    msg = redis_client.publish_message.await_args.args[1]
    assert msg.action == DeployAction.FEATURE
    assert msg.triggered_by == DeployTrigger.PO
    assert msg.head_sha == "a" * 40
    assert msg.task_id == "botrollout-abc123"

    # The record moved to published so the next tick does not republish.
    patched = api_client.update_run.await_args
    assert patched.args[0] == "botrollout-abc123"
    stored = patched.args[1]["run_metadata"][BOT_ROLLOUT_METADATA_KEY]
    assert stored["publish"] == BotRolloutPublishState.PUBLISHED.value
    assert counts["published"] == 1


@pytest.mark.asyncio
async def test_publish_failure_charges_an_attempt_and_stays_owed():
    """A failing stream write is one spent attempt; the record stays owed."""
    api_client, redis_client = _clients()
    redis_client.publish_message.side_effect = ConnectionError("stream down")
    api_client.list_bot_rollout_runs.return_value = [_make_run()]

    counts = await reconcile_bot_rollouts(api_client, redis_client)

    assert counts["publish_retrying"] == 1
    patched = api_client.update_run.await_args
    stored = patched.args[1]["run_metadata"][BOT_ROLLOUT_METADATA_KEY]
    assert stored["attempts"] == 1
    assert stored["publish"] == BotRolloutPublishState.PUBLISH_OWED.value


@pytest.mark.asyncio
async def test_exhausted_attempts_abandon_and_alert_a_human():
    api_client, redis_client = _clients()
    redis_client.publish_message.side_effect = ConnectionError("stream down")
    api_client.list_bot_rollout_runs.return_value = [_make_run(record=_owed_record(attempts=2))]

    with patch("src.tasks.bot_rollouts.notify_admins_best_effort", new=AsyncMock()):
        counts = await reconcile_bot_rollouts(api_client, redis_client)

    assert counts["publish_exhausted"] == 1
    stored = api_client.update_run.await_args.args[1]["run_metadata"][BOT_ROLLOUT_METADATA_KEY]
    assert stored["publish"] == BotRolloutPublishState.ABANDONED.value
    # One attempt was spent here (the record already carried two), and the
    # bound means a human hears about it instead of an infinite loop.
    assert redis_client.publish_message.await_count == 1


@pytest.mark.asyncio
async def test_an_owed_terminal_notification_is_delivered_once_the_deploy_ends():
    """The promise made when the conversation closed is kept by the sweep."""
    api_client, redis_client = _clients()
    record = _owed_record().model_copy(update={"publish": BotRolloutPublishState.PUBLISHED})
    notify = BotRolloutNotifyRecord(
        state=BotRolloutNotifyState.OWED,
        telegram_chat_id="777",
        owed_at=datetime.now(UTC),
    )
    api_client.list_bot_rollout_runs.return_value = [
        _make_run(status=RunStatus.COMPLETED, record=record, notify=notify.model_dump(mode="json"))
    ]

    counts = await reconcile_bot_rollouts(api_client, redis_client)

    redis_client.publish_flat.assert_awaited_once()
    fields = redis_client.publish_flat.await_args
    assert fields.args[1]["telegram_chat_id"] == "777"
    assert "live" in fields.args[1]["text"]

    stored = api_client.update_run.await_args.args[1]["run_metadata"][BOT_ROLLOUT_NOTIFY_KEY]
    assert stored["state"] == BotRolloutNotifyState.DELIVERED.value
    assert counts["notified"] == 1


@pytest.mark.asyncio
async def test_a_failed_rollout_notifies_the_owner_of_the_truth():
    api_client, redis_client = _clients()
    record = _owed_record().model_copy(update={"publish": BotRolloutPublishState.PUBLISHED})
    notify = BotRolloutNotifyRecord(
        state=BotRolloutNotifyState.OWED,
        telegram_chat_id="777",
        owed_at=datetime.now(UTC),
    )
    run = _make_run(status=RunStatus.FAILED, record=record, notify=notify.model_dump(mode="json"))
    run.error_message = "deploy workflow failed: smoke"
    api_client.list_bot_rollout_runs.return_value = [run]

    counts = await reconcile_bot_rollouts(api_client, redis_client)

    text = redis_client.publish_flat.await_args.args[1]["text"]
    assert "did NOT reach the running bot" in text
    assert "smoke" in text
    assert counts["notified"] == 1


@pytest.mark.asyncio
async def test_a_still_running_rollout_keeps_its_promise_unspent():
    api_client, redis_client = _clients()
    record = _owed_record().model_copy(update={"publish": BotRolloutPublishState.PUBLISHED})
    notify = BotRolloutNotifyRecord(
        state=BotRolloutNotifyState.OWED,
        telegram_chat_id="777",
        owed_at=datetime.now(UTC),
    )
    api_client.list_bot_rollout_runs.return_value = [
        _make_run(status=RunStatus.RUNNING, record=record, notify=notify.model_dump(mode="json"))
    ]

    counts = await reconcile_bot_rollouts(api_client, redis_client)

    redis_client.publish_flat.assert_not_called()
    assert counts["still_running"] == 1

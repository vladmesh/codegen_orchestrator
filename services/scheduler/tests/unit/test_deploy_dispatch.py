"""Tests for the scheduler's canonical deploy handoff boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.contracts.queues.deploy import DeployAction, DeployMessage
from shared.queues import DEPLOY_QUEUE
from src.tasks._recipients import Recipient
from src.tasks.deploy_dispatch import (
    DEPLOY_HANDOFF_DISPATCHED_AT_KEY,
    DEPLOY_HANDOFF_MESSAGE_KEY,
    DeployHandoff,
    deploy_run_id,
    dispatch_deploy,
    recover_deploy_handoff,
)
from src.tasks.supervisor.deploy import supervise_application_deploy_handoffs


def test_deploy_run_id_is_stable_for_one_logical_attempt():
    first = deploy_run_id("deploy-poll", "story-1", "a" * 40)
    assert first == deploy_run_id("deploy-poll", "story-1", "a" * 40)
    assert first != deploy_run_id("deploy-poll", "story-1", "b" * 40)


@pytest.mark.asyncio
async def test_dispatch_persists_run_before_story_transition_and_publish():
    api = AsyncMock()
    redis = AsyncMock()
    calls: list[str] = []

    async def create_run(run_data):
        calls.append("create")
        return SimpleNamespace(run_metadata=run_data["run_metadata"])

    async def transition(*_args):
        calls.append("transition")

    async def publish(*_args):
        calls.append("publish")
        return "1-0"

    async def update(*_args):
        calls.append("stamp")

    api.create_run_if_absent.side_effect = create_run
    api.transition_story.side_effect = transition
    api.update_run.side_effect = update
    redis.publish_message.side_effect = publish

    message = await dispatch_deploy(
        api,
        redis,
        DeployHandoff(
            run_id="deploy-poll-stable",
            project_id="project-1",
            story_id="story-1",
            recipient=Recipient(telegram_chat_id="42"),
            action=DeployAction.FEATURE,
            head_sha="a" * 40,
            deployed_commit_sha="b" * 40,
            run_metadata={"triggered_by": "pr_poll"},
            transition_action="deploy",
        ),
    )

    assert calls == ["create", "transition", "publish", "stamp"]
    run_data = api.create_run_if_absent.await_args.args[0]
    assert run_data["run_metadata"][DEPLOY_HANDOFF_MESSAGE_KEY] == message.model_dump(mode="json")
    redis.publish_message.assert_awaited_once_with(DEPLOY_QUEUE, message)
    stamped = api.update_run.await_args.args[1]["run_metadata"]
    assert DEPLOY_HANDOFF_DISPATCHED_AT_KEY in stamped


@pytest.mark.asyncio
async def test_failed_run_persistence_leaves_story_and_queue_untouched():
    api = AsyncMock()
    redis = AsyncMock()
    api.create_run_if_absent.side_effect = RuntimeError("api unavailable")

    with pytest.raises(RuntimeError, match="api unavailable"):
        await dispatch_deploy(
            api,
            redis,
            DeployHandoff(
                run_id="deploy-poll-stable",
                project_id="project-1",
                story_id="story-1",
                recipient=Recipient(telegram_chat_id="42"),
                action=DeployAction.CREATE,
                head_sha="a" * 40,
                deployed_commit_sha="b" * 40,
                run_metadata={"triggered_by": "pr_poll"},
                transition_action="deploy",
            ),
        )

    api.transition_story.assert_not_awaited()
    redis.publish_message.assert_not_awaited()
    api.update_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_queued_handoff_replays_exact_persisted_message_after_recovery_bound():
    api = AsyncMock()
    redis = AsyncMock()
    message = DeployMessage(
        task_id="deploy-poll-stable",
        project_id="project-1",
        telegram_chat_id="42",
        story_id="story-1",
        action=DeployAction.CREATE,
        head_sha="a" * 40,
        deployed_commit_sha="b" * 40,
    )
    run = SimpleNamespace(
        id=message.task_id,
        created_at=datetime.now(UTC) - timedelta(minutes=10),
        run_metadata={DEPLOY_HANDOFF_MESSAGE_KEY: message.model_dump(mode="json")},
    )

    assert await recover_deploy_handoff(api, redis, run, minimum_age_minutes=5) is True

    redis.publish_message.assert_awaited_once_with(DEPLOY_QUEUE, message)
    stamped = api.update_run.await_args.args[1]["run_metadata"]
    assert DEPLOY_HANDOFF_DISPATCHED_AT_KEY in stamped


@pytest.mark.asyncio
async def test_recent_or_already_dispatched_handoff_is_not_replayed():
    api = AsyncMock()
    redis = AsyncMock()
    message = DeployMessage(
        task_id="deploy-poll-stable",
        project_id="project-1",
        telegram_chat_id="42",
        story_id="story-1",
        action=DeployAction.CREATE,
        head_sha="a" * 40,
        deployed_commit_sha="b" * 40,
    )
    recent = SimpleNamespace(
        id=message.task_id,
        created_at=datetime.now(UTC),
        run_metadata={DEPLOY_HANDOFF_MESSAGE_KEY: message.model_dump(mode="json")},
    )
    dispatched = SimpleNamespace(
        id=message.task_id,
        created_at=datetime.now(UTC) - timedelta(minutes=10),
        run_metadata={
            DEPLOY_HANDOFF_MESSAGE_KEY: message.model_dump(mode="json"),
            DEPLOY_HANDOFF_DISPATCHED_AT_KEY: datetime.now(UTC).isoformat(),
        },
    )

    assert await recover_deploy_handoff(api, redis, recent, minimum_age_minutes=5) is False
    assert await recover_deploy_handoff(api, redis, dispatched, minimum_age_minutes=5) is False
    redis.publish_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_application_handoff_sweep_replays_only_old_storyless_runs(monkeypatch):
    api = AsyncMock()
    redis = AsyncMock()
    message = DeployMessage(
        task_id="deploy-admin-stable",
        project_id="project-1",
        unaddressed_reason="admin action",
        action=DeployAction.STOP,
        application_id=17,
    )
    old_storyless = SimpleNamespace(
        id=message.task_id,
        story_id=None,
        created_at=datetime.now(UTC) - timedelta(minutes=10),
        run_metadata={DEPLOY_HANDOFF_MESSAGE_KEY: message.model_dump(mode="json")},
    )
    story_run = SimpleNamespace(
        id="deploy-story",
        story_id="story-1",
        created_at=datetime.now(UTC) - timedelta(minutes=10),
        run_metadata={DEPLOY_HANDOFF_MESSAGE_KEY: message.model_dump(mode="json")},
    )
    api.list_runs.return_value = [old_storyless, story_run]
    monkeypatch.setattr(
        "src.tasks.supervisor.deploy._qa_handoff_recovery_minutes",
        lambda: 5,
    )

    result = await supervise_application_deploy_handoffs(api, redis)

    assert result == {"recovered": 1}
    redis.publish_message.assert_awaited_once_with(DEPLOY_QUEUE, message)
    api.update_run.assert_awaited_once()

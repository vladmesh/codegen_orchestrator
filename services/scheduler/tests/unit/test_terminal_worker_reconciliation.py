from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.contracts.dto.story import StoryStatus
from shared.queues import STORY_WORKERS_KEY, WORKER_COMMANDS
from src.tasks.terminal_worker_reconciliation import reconcile_terminal_story_workers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status", [StoryStatus.COMPLETED, StoryStatus.FAILED, StoryStatus.ARCHIVED]
)
async def test_terminal_story_reconciliation_requests_every_owned_worker_and_retries_until_gone(
    terminal_status,
):
    story = SimpleNamespace(id="story-terminal", status=terminal_status)
    api = AsyncMock()
    api.get_stories_by_status.side_effect = lambda status: (
        [story] if status == terminal_status else []
    )
    redis = AsyncMock()
    redis.scan_iter = lambda **_: _keys(
        "worker:meta:developer", "worker:meta:qa-fix", "worker:meta:other"
    )
    metadata = {
        "worker:meta:developer": {"story_id": "story-terminal"},
        "worker:meta:qa-fix": {"story_id": "story-terminal"},
        "worker:meta:other": {"story_id": "story-live"},
    }
    redis.hgetall.side_effect = metadata.__getitem__
    redis.hget.side_effect = lambda key, *args: "developer" if key == STORY_WORKERS_KEY else None
    client = SimpleNamespace(redis=redis, publish=AsyncMock())

    assert await reconcile_terminal_story_workers(api, client) == 2

    commands = [call.args[1] for call in client.publish.await_args_list]
    assert [command["worker_id"] for command in commands] == ["developer", "qa-fix"]
    assert all(call.args[0] == WORKER_COMMANDS for call in client.publish.await_args_list)
    redis.hdel.assert_not_awaited()

    metadata["worker:meta:developer"] = {}
    metadata["worker:meta:qa-fix"] = {}
    redis.scan_iter = lambda **_: _keys("worker:meta:other")
    redis.hget.side_effect = lambda key, *args: "developer" if key == STORY_WORKERS_KEY else None
    assert await reconcile_terminal_story_workers(api, client) == 0
    redis.hdel.assert_awaited_once_with(STORY_WORKERS_KEY, "story-terminal")


async def _keys(*values):
    for value in values:
        yield value


@pytest.mark.asyncio
async def test_publish_failure_keeps_retryable_worker_ownership():
    api = AsyncMock()
    api.get_stories_by_status.side_effect = lambda status: (
        [SimpleNamespace(id="story-terminal", status=status)]
        if status == StoryStatus.FAILED
        else []
    )
    redis = AsyncMock()
    redis.scan_iter = lambda **_: _keys("worker:meta:developer")
    redis.hgetall.return_value = {"story_id": "story-terminal"}
    redis.hget.return_value = None
    client = SimpleNamespace(redis=redis, publish=AsyncMock(side_effect=RuntimeError("redis down")))

    assert await reconcile_terminal_story_workers(api, client) == 0
    redis.delete.assert_not_awaited()
    redis.hdel.assert_not_awaited()

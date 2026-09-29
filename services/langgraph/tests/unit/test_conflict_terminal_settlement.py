"""Terminal Run persistence may precede settlement, never a Task-only ending."""

from unittest.mock import AsyncMock, patch

import pytest

from src.consumers import _base, engineering_result_handler as handler


@pytest.mark.asyncio
@pytest.mark.parametrize("gave_up", [False, True])
async def test_interrupted_conflict_delivery_leaves_terminal_run_for_native_supervision(gave_up):
    api = AsyncMock()
    persisted = {}

    async def persist(path, *, json):
        assert path == "runs/attempt"
        persisted.update(json)

    async def unavailable(*args):
        assert persisted["status"] == "failed"
        assert persisted["result"]["engineering_status"] == ("gave_up" if gave_up else "failed")
        raise OSError("settlement unavailable before commit")

    api.patch.side_effect = persist
    api.get.return_value = persisted
    settle = AsyncMock(side_effect=unavailable)
    with (
        patch.object(handler, "api_client", api),
        patch.object(_base, "api_client", api),
        patch.object(handler, "settle_pr_repair_attempt", settle),
    ):
        with pytest.raises(OSError, match="before commit"):
            if gave_up:
                await handler.handle_worker_gave_up(
                    "attempt",
                    "project",
                    "pr-conflict-task",
                    "story",
                    "Cannot repair",
                    "",
                    AsyncMock(),
                )
            else:
                await handler.fail_job(
                    "attempt",
                    "Technical failure",
                    "pr-conflict-task",
                    redis=AsyncMock(),
                    story_id="story",
                    turn_result_consumed=True,
                )
        # Terminal reclaim deliberately ACKs immutable work; stuck supervision
        # owns recovery of the still-discoverable Task through the scoped API.
        assert await _base._check_message_staleness({"task_id": "attempt", "story_id": "story"})
    settle.assert_awaited_once()
    api.post.assert_not_awaited()
    api.transition_story.assert_not_awaited()

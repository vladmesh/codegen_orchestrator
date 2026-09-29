"""The attempted turn starts after checkout's native publication, not before it."""

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.queues.worker import WorkerOwnership

OWNERSHIP = WorkerOwnership(
    project_id="project", run_id="init", attempt_id="eng-1", story_id="story"
)
RUN = {
    "id": "eng-1",
    "project_id": "project",
    "story_id": "story",
    "type": "engineering",
    "run_metadata": {"initiating_run_id": "init"},
}


@pytest.mark.asyncio
async def test_prepared_remote_head_is_persisted_before_the_turn():
    from src.clients.worker_spawner import reconcile_prepared_baseline

    redis = AsyncMock()
    redis.hgetall.side_effect = lambda key: (
        {}
        if "active-turn" in key
        else {
            **OWNERSHIP.as_redis_meta(),
            "prepared_head_sha": "b" * 40,
        }
    )
    with (
        patch("src.clients.api.api_client.get", AsyncMock(return_value=RUN)),
        patch("src.clients.api.api_client.patch", new_callable=AsyncMock) as write,
    ):
        assert (
            await reconcile_prepared_baseline(redis, OWNERSHIP, "worker-1")
        ).pre_attempt_head_sha == "b" * 40
    redis.hgetall.assert_any_await("worker:meta:worker-1")
    write.assert_awaited_once_with(
        "runs/eng-1",
        json={
            "run_metadata": {
                "worker_id": "worker-1",
                "pre_attempt_head_sha": "b" * 40,
                "prepared_checkout": {
                    "worker_id": "worker-1",
                    "attempt_id": "eng-1",
                    "head_sha": "b" * 40,
                },
            }
        },
    )


@pytest.mark.asyncio
async def test_missing_prepared_ref_fails_before_recording_or_sending_work():
    from src.clients.worker_spawner import reconcile_prepared_baseline

    redis = AsyncMock()
    redis.hgetall.return_value = OWNERSHIP.as_redis_meta()
    with (
        patch("src.clients.api.api_client.get", AsyncMock(return_value=RUN)),
        patch("src.clients.api.api_client.patch", new_callable=AsyncMock) as write,
        pytest.raises(RuntimeError, match="prepared checkout"),
    ):
        await reconcile_prepared_baseline(redis, OWNERSHIP, "worker-1")
    write.assert_not_awaited()

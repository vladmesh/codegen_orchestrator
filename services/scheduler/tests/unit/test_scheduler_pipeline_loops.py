"""Contracts for independently supervised scheduler-pipeline responsibility loops."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_scaffold_loop_continues_after_failure_and_owns_redis(monkeypatch):
    import asyncio

    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:9")
    from src.clients import api as api_module
    from src.tasks import scaffold_loop

    api = AsyncMock()
    redis = AsyncMock()
    log = MagicMock()
    trigger = AsyncMock(side_effect=[RuntimeError("scaffold failed"), 2])
    monkeypatch.setattr(api_module, "api_client", api)
    monkeypatch.setattr(scaffold_loop, "RedisStreamClient", lambda: redis)
    monkeypatch.setattr(scaffold_loop, "trigger_scaffolds", trigger)
    monkeypatch.setattr(scaffold_loop, "_scaffold_interval", lambda: 0)
    monkeypatch.setattr(scaffold_loop, "logger", log)
    monkeypatch.setattr(
        scaffold_loop.asyncio,
        "sleep",
        AsyncMock(side_effect=[None, asyncio.CancelledError]),
    )

    with pytest.raises(asyncio.CancelledError):
        await scaffold_loop.scaffold_loop()

    assert trigger.await_count == 2
    trigger.assert_awaited_with(api, redis)
    log.exception.assert_called_once_with("scaffold_cycle_error")
    log.info.assert_any_call("scaffold_cycle", scaffolds_triggered=2)
    redis.connect.assert_awaited_once()
    redis.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_story_completion_loop_continues_after_failure_and_owns_redis(monkeypatch):
    import asyncio

    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:9")
    from src.clients import api as api_module
    from src.tasks import story_completion_loop

    api = AsyncMock()
    redis = AsyncMock()
    log = MagicMock()
    complete = AsyncMock(side_effect=[RuntimeError("completion failed"), 1])
    monkeypatch.setattr(api_module, "api_client", api)
    monkeypatch.setattr(story_completion_loop, "RedisStreamClient", lambda: redis)
    monkeypatch.setattr(story_completion_loop, "complete_stories", complete)
    monkeypatch.setattr(story_completion_loop, "_story_completion_interval", lambda: 0)
    monkeypatch.setattr(story_completion_loop, "logger", log)
    monkeypatch.setattr(
        story_completion_loop.asyncio,
        "sleep",
        AsyncMock(side_effect=[None, asyncio.CancelledError]),
    )

    with pytest.raises(asyncio.CancelledError):
        await story_completion_loop.story_completion_loop()

    assert complete.await_count == 2
    complete.assert_awaited_with(api, redis)
    log.exception.assert_called_once_with("story_completion_cycle_error")
    log.info.assert_any_call("story_completion_cycle", stories_completed=1)
    redis.connect.assert_awaited_once()
    redis.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifecycle_supervisors_keep_historical_order_but_fail_independently(monkeypatch):
    from src.tasks import lifecycle_supervision_loop

    api = AsyncMock()
    redis = AsyncMock()
    log = MagicMock()
    calls: list[str] = []

    def sweep(name: str, result=None, error: Exception | None = None):
        async def run(*_args):
            calls.append(name)
            if error is not None:
                raise error
            return result or {}

        return AsyncMock(side_effect=run)

    stuck_stories = sweep("stuck_stories", error=RuntimeError("poison story"))
    stuck_tasks = sweep("stuck_tasks", {"timed_out": 1})
    failed_tasks = sweep("failed_tasks", {"retried": 2})
    waiting_resources = sweep("waiting_resources", {"resumed": 3})
    application_handoffs = sweep("application_deploy_handoffs", {"recovered": 6})
    deploying = sweep("deploying", {"tested": 4})
    waiting_secret = sweep("waiting_user_secret", {"redispatched": 5})

    monkeypatch.setattr(lifecycle_supervision_loop, "supervise_stuck_stories", stuck_stories)
    monkeypatch.setattr(lifecycle_supervision_loop, "supervise_stuck_tasks", stuck_tasks)
    monkeypatch.setattr(lifecycle_supervision_loop, "supervise_failed_tasks", failed_tasks)
    monkeypatch.setattr(
        lifecycle_supervision_loop,
        "supervise_waiting_resource_tasks",
        waiting_resources,
    )
    monkeypatch.setattr(
        lifecycle_supervision_loop,
        "supervise_application_deploy_handoffs",
        application_handoffs,
    )
    monkeypatch.setattr(lifecycle_supervision_loop, "supervise_deploying_stories", deploying)
    monkeypatch.setattr(
        lifecycle_supervision_loop,
        "supervise_waiting_user_secret_stories",
        waiting_secret,
    )
    monkeypatch.setattr(lifecycle_supervision_loop, "logger", log)

    counts = await lifecycle_supervision_loop.supervise_lifecycle_once(api, redis)

    assert calls == [
        "stuck_stories",
        "stuck_tasks",
        "failed_tasks",
        "waiting_resources",
        "application_deploy_handoffs",
        "deploying",
        "waiting_user_secret",
    ]
    assert counts == {
        "stuck_tasks_timed_out": 1,
        "failed_tasks_retried": 2,
        "waiting_resources_resumed": 3,
        "application_deploy_handoffs_recovered": 6,
        "deploying_tested": 4,
        "waiting_user_secret_redispatched": 5,
    }
    for supervisor in (
        stuck_stories,
        stuck_tasks,
        failed_tasks,
        waiting_resources,
        application_handoffs,
        deploying,
        waiting_secret,
    ):
        supervisor.assert_awaited_once_with(api, redis)
    log.exception.assert_called_once_with(
        "lifecycle_supervision_sweep_error",
        sweep="stuck_stories",
    )
    log.info.assert_called_once_with("lifecycle_supervision_cycle", **counts)


@pytest.mark.asyncio
async def test_lifecycle_supervision_loop_owns_redis_lifecycle(monkeypatch):
    import asyncio

    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:9")
    from src.clients import api as api_module
    from src.tasks import lifecycle_supervision_loop

    api = AsyncMock()
    redis = AsyncMock()
    cycle = AsyncMock(return_value={})
    monkeypatch.setattr(api_module, "api_client", api)
    monkeypatch.setattr(lifecycle_supervision_loop, "RedisStreamClient", lambda: redis)
    monkeypatch.setattr(lifecycle_supervision_loop, "supervise_lifecycle_once", cycle)
    monkeypatch.setattr(lifecycle_supervision_loop, "_lifecycle_supervision_interval", lambda: 0)
    monkeypatch.setattr(
        lifecycle_supervision_loop.asyncio,
        "sleep",
        AsyncMock(side_effect=asyncio.CancelledError),
    )

    with pytest.raises(asyncio.CancelledError):
        await lifecycle_supervision_loop.lifecycle_supervision_loop()

    cycle.assert_awaited_once_with(api, redis)
    redis.connect.assert_awaited_once()
    redis.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_qa_routing_loop_continues_after_failure_and_owns_redis(monkeypatch):
    import asyncio

    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:9")
    from src.clients import api as api_module
    from src.tasks import qa_routing_loop

    api = AsyncMock()
    redis = AsyncMock()
    log = MagicMock()
    route = AsyncMock(side_effect=[RuntimeError("qa routing failed"), {"completed": 1}])
    monkeypatch.setattr(api_module, "api_client", api)
    monkeypatch.setattr(qa_routing_loop, "RedisStreamClient", lambda: redis)
    monkeypatch.setattr(qa_routing_loop, "supervise_testing_stories", route)
    monkeypatch.setattr(qa_routing_loop, "_qa_routing_interval", lambda: 0)
    monkeypatch.setattr(qa_routing_loop, "logger", log)
    monkeypatch.setattr(
        qa_routing_loop.asyncio,
        "sleep",
        AsyncMock(side_effect=[None, asyncio.CancelledError]),
    )

    with pytest.raises(asyncio.CancelledError):
        await qa_routing_loop.qa_routing_loop()

    assert route.await_count == 2
    route.assert_awaited_with(api, redis)
    log.exception.assert_called_once_with("qa_routing_cycle_error")
    log.info.assert_any_call("qa_routing_cycle", completed=1)
    redis.connect.assert_awaited_once()
    redis.close.assert_awaited_once()

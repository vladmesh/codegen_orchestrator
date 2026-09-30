"""Behavior contracts for the independent PR/CI scheduler loop."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_pr_ci_loop_polls_both_paths_and_closes_redis(monkeypatch):
    from src.tasks import pr_ci_loop

    redis = MagicMock()
    redis.connect = AsyncMock()
    redis.close = AsyncMock()
    merged = AsyncMock(return_value=2)
    failures = AsyncMock(return_value=3)
    log = MagicMock()
    sleeps = 0

    async def stop_after_one_cycle(_seconds):
        nonlocal sleeps
        sleeps += 1
        raise asyncio.CancelledError

    monkeypatch.setattr(pr_ci_loop, "RedisStreamClient", MagicMock(return_value=redis))
    monkeypatch.setattr(pr_ci_loop, "poll_merged_prs", merged)
    monkeypatch.setattr(pr_ci_loop, "poll_ci_failures", failures)
    monkeypatch.setattr(pr_ci_loop, "logger", log)
    monkeypatch.setattr(pr_ci_loop.asyncio, "sleep", stop_after_one_cycle)
    monkeypatch.setattr(pr_ci_loop, "_pr_ci_interval", lambda: 30)

    with pytest.raises(asyncio.CancelledError):
        await pr_ci_loop.pr_ci_loop()

    redis.connect.assert_awaited_once()
    redis.close.assert_awaited_once()
    merged.assert_awaited_once()
    failures.assert_awaited_once()
    assert sleeps == 1
    log.info.assert_any_call("pr_ci_started", interval=30)
    log.info.assert_any_call("pr_ci_cycle", prs_merged=2, ci_failures_routed=3)
    log.info.assert_any_call("pr_ci_stopped")


@pytest.mark.asyncio
async def test_pr_merge_failure_does_not_skip_ci_failure_routing(monkeypatch):
    from src.tasks import pr_ci_loop

    redis = MagicMock()
    redis.connect = AsyncMock()
    redis.close = AsyncMock()
    merged = AsyncMock(side_effect=RuntimeError("github unavailable"))
    failures = AsyncMock(return_value=4)
    log = MagicMock()

    async def stop_after_one_cycle(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(pr_ci_loop, "RedisStreamClient", MagicMock(return_value=redis))
    monkeypatch.setattr(pr_ci_loop, "poll_merged_prs", merged)
    monkeypatch.setattr(pr_ci_loop, "poll_ci_failures", failures)
    monkeypatch.setattr(pr_ci_loop, "logger", log)
    monkeypatch.setattr(pr_ci_loop.asyncio, "sleep", stop_after_one_cycle)
    monkeypatch.setattr(pr_ci_loop, "_pr_ci_interval", lambda: 30)

    with pytest.raises(asyncio.CancelledError):
        await pr_ci_loop.pr_ci_loop()

    merged.assert_awaited_once()
    failures.assert_awaited_once()
    log.exception.assert_called_once_with("pr_merge_poll_error")
    log.info.assert_any_call("pr_ci_cycle", prs_merged=0, ci_failures_routed=4)

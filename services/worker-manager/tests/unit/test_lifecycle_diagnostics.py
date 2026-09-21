from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from fakeredis import aioredis
import pytest

from src.lifecycle_diagnostics import collect_worker_lifecycle_diagnostics


@pytest.mark.asyncio
async def test_rollout_diagnostic_counts_remains_and_reports_oldest_known_age():
    observed = datetime(2026, 9, 13, tzinfo=UTC)
    redis = aioredis.FakeRedis(decode_responses=True)
    await redis.hset("worker:status:terminal-known", mapping={"status": "FAILED"})
    await redis.hset(
        "worker:meta:terminal-known",
        mapping={
            "owned_at": (observed - timedelta(minutes=12)).isoformat(),
            "story_id": "story-1",
        },
    )
    await redis.hset("worker:status:terminal-legacy", mapping={"status": "STOPPED"})
    await redis.hset("worker:status:live", mapping={"status": "RUNNING"})
    await redis.set("workspace:lock:ownerless-project", "legacy-worker")
    await redis.set("workspace:lock:owned-project", "owned-worker")
    await redis.hset(
        "worker:meta:owned-worker",
        mapping={"story_id": "story-live", "owned_at": observed.isoformat()},
    )

    docker = AsyncMock()
    docker.inspect_container.side_effect = RuntimeError("docker unavailable")
    diagnostic = await collect_worker_lifecycle_diagnostics(redis, docker, observed_at=observed)

    assert diagnostic.terminal_worker_remains.count == 2
    assert diagnostic.terminal_worker_remains.oldest_age_seconds == 720
    assert diagnostic.terminal_worker_remains.unknown_age_count == 1
    assert diagnostic.ownerless_project_locks.count == 1
    assert diagnostic.ownerless_project_locks.identifiers == ["ownerless-project"]
    assert diagnostic.ownerless_project_locks.unknown_age_count == 1


@pytest.mark.asyncio
async def test_ownerless_lock_uses_legacy_worker_age_when_available():
    redis = aioredis.FakeRedis(decode_responses=True)
    observed = datetime(2026, 9, 13, 12, tzinfo=UTC)
    await redis.hset("worker:status:terminal", mapping={"status": "FAILED"})
    await redis.hset(
        "worker:meta:terminal",
        mapping={
            "project_id": "project-old",
        },
    )
    await redis.set("workspace:lock:project-old", "terminal")
    await redis.set("workspace:lock:project-unknown", "missing-worker")

    docker = AsyncMock()
    docker.inspect_container.side_effect = lambda name: (
        {"Created": (observed - timedelta(hours=2)).isoformat()}
        if name == "worker-terminal"
        else (_ for _ in ()).throw(RuntimeError("container unavailable"))
    )
    diagnostic = await collect_worker_lifecycle_diagnostics(redis, docker, observed_at=observed)

    assert diagnostic.terminal_worker_remains.count == 1
    assert diagnostic.terminal_worker_remains.oldest_age_seconds == 7200
    assert diagnostic.terminal_worker_remains.identifiers == ["terminal"]
    assert diagnostic.ownerless_project_locks.count == 2
    assert diagnostic.ownerless_project_locks.oldest_age_seconds == 7200
    assert diagnostic.ownerless_project_locks.unknown_age_count == 1
    assert diagnostic.ownerless_project_locks.identifiers == [
        "project-old",
        "project-unknown",
    ]
    docker.inspect_container.assert_any_await("worker-terminal")

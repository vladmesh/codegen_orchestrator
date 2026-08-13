"""How a worker ended is written down by whoever removes it.

Labels make a dead worker attributable; nothing makes a *removed* one
attributable, because Docker forgets a removed container and its labels with it.
`delete_worker` removes the container and then deletes `worker:meta:<id>`, so
between those two moments the whole ending is readable and one moment later
none of it exists. These tests hold the capture at that point, and hold the
precedence that goes with it: the capture is bounded, it never raises at its
caller, and a worker whose ending cannot be read is still removed with the
reason recorded rather than dropped.

The same property against a real daemon — a worker created and deleted through
the ordinary path before anything observes it, arriving in its run's artifact
with an exit code — is in `tests/integration/backend/test_run_evidence_by_label.py`.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from fakeredis import aioredis
import pytest

from shared.contracts.queues.worker import WorkerOwnership
from shared.contracts.worker_evidence import (
    REMOVAL_LOG_TAIL_MAX_CHARS,
    RemovedWorkerEvidence,
    removed_worker_evidence_key,
)
from src.config import settings
from src.manager import WorkerManager

pytestmark = pytest.mark.asyncio

OWNERSHIP = WorkerOwnership(project_id="proj-alpha", run_id="live-alpha", attempt_id="eng-alpha-1")
WORKER_ID = "dev-alpha-1"
CONTAINER = f"{settings.WORKER_IMAGE_PREFIX}-{WORKER_ID}"

API_KEY = "sk-ant-not-a-real-key-0001"


def container_payload(*, status: str = "exited", exit_code: int = 137, environment=()) -> dict:
    return {
        "Image": "sha256:" + "1" * 64,
        "Config": {
            "Image": "worker-base-codex:latest",
            "Env": [
                "WORKER_AGENT_TYPE=codex",
                "WORKER_TYPE=developer",
                f"ANTHROPIC_API_KEY={API_KEY}",
                *environment,
            ],
        },
        "State": {
            "Status": status,
            "Running": status == "running",
            "OOMKilled": False,
            "ExitCode": exit_code,
            "StartedAt": "2026-08-13T10:00:00Z",
            "FinishedAt": "2026-08-13T10:00:42Z",
            "Error": "",
        },
        "Mounts": [
            {"Destination": "/workspace", "Source": "/data/workspaces/repo-1"},
            {
                "Destination": "/artifacts/worker-transcripts",
                "Source": "/data/worker-transcripts",
            },
        ],
    }


def docker_double(order: list[str], payload: dict | None = None, logs: str = "wrapper line\n"):
    """A docker client that records the order of what the deletion asks it."""
    docker = MagicMock()

    async def inspect(container_id):
        order.append(f"inspect:{container_id}")
        return payload if payload is not None else container_payload()

    async def read_logs(container_id, tail=50):
        order.append(f"logs:{container_id}")
        return logs

    async def remove(container_id, force=False, v=False):
        order.append(f"remove:{container_id}")

    docker.inspect_container = AsyncMock(side_effect=inspect)
    docker.read_container_logs = AsyncMock(side_effect=read_logs)
    docker.remove_container = AsyncMock(side_effect=remove)
    docker.remove_network = AsyncMock()
    return docker


async def owned_worker(redis, *, worker_type: str = "developer", ownership=OWNERSHIP) -> None:
    """The Redis record a worker of this run has by the time it is deleted."""
    mapping = {"worker_type": worker_type}
    if ownership is not None:
        mapping.update(ownership.as_redis_meta())
    await redis.hset(f"worker:meta:{WORKER_ID}", mapping=mapping)


async def stored_record(redis, run_id: str = OWNERSHIP.run_id) -> RemovedWorkerEvidence | None:
    raw = await redis.hget(removed_worker_evidence_key(run_id), WORKER_ID)
    return RemovedWorkerEvidence.model_validate_json(raw) if raw else None


async def test_the_ending_is_captured_before_the_container_is_removed():
    """Read it while it exists, or never: removal is the point of no return."""
    redis = aioredis.FakeRedis(decode_responses=True)
    order: list[str] = []
    manager = WorkerManager(redis=redis, docker_client=docker_double(order))
    await owned_worker(redis)

    await manager.delete_worker(WORKER_ID, reason="failed")

    assert order == [f"inspect:{CONTAINER}", f"logs:{CONTAINER}", f"remove:{CONTAINER}"]
    record = await stored_record(redis)
    assert record.exit_code.value == 137
    assert record.log_tail.value == "wrapper line\n"
    assert record.agent_type.value == "codex"
    assert record.worker_type.value == "developer"
    assert record.image.value["tag"] == "worker-base-codex:latest"
    assert record.state.value["running"] is False
    assert record.delete_reason == "failed"
    assert record.ownership == OWNERSHIP
    assert record.container == CONTAINER
    # Where a Codex exit is attributed afterwards, recorded while the mount was
    # still declared: the transcript outlives the container, the path to it does
    # not unless somebody writes it down.
    assert record.transcript_dir.value == f"/data/worker-transcripts/{WORKER_ID}"


async def test_the_record_outlives_the_metadata_the_deletion_erases():
    """`delete_worker` deletes `worker:meta`. The evidence is not in it."""
    redis = aioredis.FakeRedis(decode_responses=True)
    manager = WorkerManager(redis=redis, docker_client=docker_double([]))
    await owned_worker(redis)

    await manager.delete_worker(WORKER_ID)

    assert await redis.hgetall(f"worker:meta:{WORKER_ID}") == {}
    assert await stored_record(redis) is not None
    ttl = await redis.ttl(removed_worker_evidence_key(OWNERSHIP.run_id))
    assert 0 < ttl <= settings.WORKER_REMOVAL_EVIDENCE_TTL_SECONDS


async def test_one_runs_record_never_collects_another_runs_worker():
    """The record is filed under the worker's own run, not the deleter's."""
    redis = aioredis.FakeRedis(decode_responses=True)
    manager = WorkerManager(redis=redis, docker_client=docker_double([]))
    await owned_worker(redis)

    await manager.delete_worker(WORKER_ID)

    assert await redis.hgetall(removed_worker_evidence_key("live-beta")) == {}
    assert await stored_record(redis) is not None


async def test_a_qa_executor_is_recorded_as_one():
    """The role comes off the record worker-manager held, not a name prefix."""
    redis = aioredis.FakeRedis(decode_responses=True)
    docker = docker_double([])
    manager = WorkerManager(redis=redis, docker_client=docker)
    await owned_worker(redis, worker_type="qa")

    await manager.delete_worker(WORKER_ID)

    assert (await stored_record(redis)).worker_type.value == "qa"


async def test_a_container_that_cannot_be_read_is_still_removed_and_still_recorded():
    """A capture failure is a finding, not an omission and not a stuck deletion."""
    redis = aioredis.FakeRedis(decode_responses=True)
    order: list[str] = []
    docker = docker_double(order)
    docker.inspect_container = AsyncMock(side_effect=RuntimeError("daemon said no"))
    manager = WorkerManager(redis=redis, docker_client=docker)
    await owned_worker(redis)

    await manager.delete_worker(WORKER_ID)

    assert order == [f"remove:{CONTAINER}"]
    record = await stored_record(redis)
    for fact in (record.exit_code, record.log_tail, record.image, record.transcript_dir):
        assert fact.value is None
        assert "daemon said no" in fact.missed_reason
    # The worker is still named, and its type still comes from its metadata:
    # what was lost is how it ended, not that it existed.
    assert record.worker_type.value == "developer"


async def test_an_unreadable_log_does_not_cost_the_exit_code():
    """Two facts, captured independently: losing one never loses the other."""
    redis = aioredis.FakeRedis(decode_responses=True)
    docker = docker_double([])
    docker.read_container_logs = AsyncMock(side_effect=RuntimeError("log driver is not local"))
    manager = WorkerManager(redis=redis, docker_client=docker)
    await owned_worker(redis)

    await manager.delete_worker(WORKER_ID)

    record = await stored_record(redis)
    assert record.exit_code.value == 137
    assert "log driver is not local" in record.log_tail.missed_reason


async def test_a_capture_that_runs_long_is_cut_off_and_the_removal_proceeds(monkeypatch):
    """Cleanup is never wedged by observability, however slow the daemon is."""
    redis = aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(settings, "WORKER_REMOVAL_EVIDENCE_TIMEOUT_SECONDS", 0.05)
    order: list[str] = []
    docker = docker_double(order)

    async def hang(container_id):
        await asyncio.sleep(30)

    docker.inspect_container = AsyncMock(side_effect=hang)
    manager = WorkerManager(redis=redis, docker_client=docker)
    await owned_worker(redis)

    await asyncio.wait_for(manager.delete_worker(WORKER_ID), timeout=5)

    assert order == [f"remove:{CONTAINER}"]
    assert "TimeoutError" in (await stored_record(redis)).exit_code.missed_reason


async def test_a_record_that_cannot_be_stored_never_fails_the_deletion():
    """The durable half is the half that can fail. Removal is not conditional on it."""
    redis = aioredis.FakeRedis(decode_responses=True)
    order: list[str] = []
    docker = docker_double(order)
    manager = WorkerManager(redis=redis, docker_client=docker)
    await owned_worker(redis)
    original_hset = redis.hset

    async def refuse(key, *args, **kwargs):
        if key.startswith("worker:evidence:"):
            raise RuntimeError("redis is out of memory")
        return await original_hset(key, *args, **kwargs)

    redis.hset = refuse

    await manager.delete_worker(WORKER_ID)

    assert f"remove:{CONTAINER}" in order


async def test_a_record_that_cannot_be_stored_keeps_the_workers_last_durable_name():
    """The two destructive steps are not equal, and only one of them may proceed.

    The container goes — cleanup is never wedged by observability. But deleting
    `worker:meta:<id>` after a failed store would leave no source at all able to
    name this worker, which is the silent omission this evidence exists to end.
    So the metadata stays, and a leaked key is the good failure: the run's
    ownership manifest still reaches it, and a label sweep collects it later.
    """
    redis = aioredis.FakeRedis(decode_responses=True)
    order: list[str] = []
    docker = docker_double(order)
    manager = WorkerManager(redis=redis, docker_client=docker)
    await owned_worker(redis)
    original_hset = redis.hset

    async def refuse(key, *args, **kwargs):
        if key.startswith("worker:evidence:"):
            raise RuntimeError("redis is out of memory")
        return await original_hset(key, *args, **kwargs)

    redis.hset = refuse

    await manager.delete_worker(WORKER_ID, reason="failed")

    assert f"remove:{CONTAINER}" in order
    assert await stored_record(redis) is None
    meta = await redis.hgetall(f"worker:meta:{WORKER_ID}")
    assert meta["run_id"] == OWNERSHIP.run_id
    # Only the name is kept. Everything else this deletion erases still goes.
    assert await redis.hgetall(f"worker:status:{WORKER_ID}") == {}


async def test_a_worker_whose_metadata_names_no_owner_is_still_removed():
    """There is no run to file it under, and that must not stop the cleanup."""
    redis = aioredis.FakeRedis(decode_responses=True)
    order: list[str] = []
    manager = WorkerManager(redis=redis, docker_client=docker_double(order))
    await owned_worker(redis, ownership=None)

    await manager.delete_worker(WORKER_ID)

    assert order == [f"remove:{CONTAINER}"]
    assert await redis.keys("worker:evidence:*") == []


async def test_a_worker_removed_while_it_still_ran_says_so_instead_of_exit_zero():
    """`delete_worker` force-removes. A killed worker did not exit cleanly."""
    redis = aioredis.FakeRedis(decode_responses=True)
    docker = docker_double([], payload=container_payload(status="running", exit_code=0))
    manager = WorkerManager(redis=redis, docker_client=docker)
    await owned_worker(redis)

    await manager.delete_worker(WORKER_ID, reason="timeout")

    record = await stored_record(redis)
    assert record.exit_code.value is None
    assert "still running when it was removed" in record.exit_code.missed_reason
    # The tail is the whole point in this case: it is all there will ever be.
    assert record.log_tail.value == "wrapper line\n"


async def test_the_tail_is_redacted_against_the_containers_own_secrets():
    """A container log that echoed a credential must not persist it."""
    redis = aioredis.FakeRedis(decode_responses=True)
    docker = docker_double([], logs=f"agent failed: auth={API_KEY}\n")
    manager = WorkerManager(redis=redis, docker_client=docker)
    await owned_worker(redis)

    await manager.delete_worker(WORKER_ID)

    tail = (await stored_record(redis)).log_tail.value
    assert API_KEY not in tail
    assert "[redacted]" in tail


async def test_the_tail_is_bounded_and_keeps_the_end():
    """The last lines before a worker died are the ones that say why."""
    redis = aioredis.FakeRedis(decode_responses=True)
    noise = "x" * (REMOVAL_LOG_TAIL_MAX_CHARS * 3)
    docker = docker_double([], logs=f"{noise}\nthe last thing it said")
    manager = WorkerManager(redis=redis, docker_client=docker)
    await owned_worker(redis)

    await manager.delete_worker(WORKER_ID)

    tail = (await stored_record(redis)).log_tail.value
    assert len(tail) == REMOVAL_LOG_TAIL_MAX_CHARS
    assert tail.endswith("the last thing it said")


async def test_the_stored_record_is_one_line_of_json():
    """Its readers pull it back a line at a time through redis-cli."""
    redis = aioredis.FakeRedis(decode_responses=True)
    docker = docker_double([], logs="first line\nsecond line\n")
    manager = WorkerManager(redis=redis, docker_client=docker)
    await owned_worker(redis)

    await manager.delete_worker(WORKER_ID)

    raw = await redis.hget(removed_worker_evidence_key(OWNERSHIP.run_id), WORKER_ID)
    assert "\n" not in raw
    assert json.loads(raw)["log_tail"]["value"] == "first line\nsecond line\n"

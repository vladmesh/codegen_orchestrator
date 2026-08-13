"""The workspace lock belongs to the worker that acquired it, and to no other.

Ownership is stamped on every worker before anything can be created, so a worker
that dies immediately is still attributable. That made `project_id` present on
workers that never took the project's workspace — a refused developer worker, a
QA executor — and reading it as "this worker holds the lock" releases a live
worker's checkout under it. These tests hold the two facts apart: ownership says
who a worker belonged to, the holder fact says what it acquired.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fakeredis import aioredis

from shared.contracts.dto.worker import WorkerStatus
from shared.contracts.queues.worker import WorkerOwnership
from shared.redis import decode_redis_fields

from src.manager import WORKSPACE_LOCK_FIELD, WorkerManager


PROJECT = "proj-shared-checkout"


def _make_docker_mock():
    docker = MagicMock()
    docker.image_exists = AsyncMock(return_value=True)
    docker.get_image_label = AsyncMock(return_value="basehash0001")
    docker.build_image = AsyncMock()
    docker.remove_container = AsyncMock()
    docker.create_network = AsyncMock()
    docker.connect_network = AsyncMock()
    docker.remove_network = AsyncMock()
    docker.exec_in_container = AsyncMock(return_value=(0, ""))
    docker.get_container_logs = AsyncMock(return_value="")
    container = MagicMock()
    container.id = "container-abc"
    docker.run_container = AsyncMock(return_value=container)
    return docker


def _ownership(run_id: str, attempt_id: str) -> WorkerOwnership:
    return WorkerOwnership(project_id=PROJECT, run_id=run_id, attempt_id=attempt_id)


async def _create(manager: WorkerManager, worker_id: str, run_id: str) -> str:
    with patch(
        "src.manager.workspace_mod.get_scaffolded_workspace",
        return_value=(Path("/tmp/ws/repo-1"), True),
    ):
        return await manager.create_worker_with_capabilities(
            worker_id=worker_id,
            capabilities=["GIT"],
            base_image="worker-base:latest",
            ownership=_ownership(run_id, f"attempt-{run_id}"),
            repo_id="repo-1",
        )


def _compose_runner_patch():
    runner = MagicMock()
    runner.run = AsyncMock(return_value=(0, "", ""))
    patcher = patch("src.manager.ComposeRunner")
    return patcher, runner


@pytest.fixture
def docker():
    return _make_docker_mock()


@pytest.mark.asyncio
async def test_a_rejected_worker_cleanup_leaves_the_live_workers_lock_alone(docker):
    """The whole failure, end to end: A holds, B is refused, B is deleted, A still holds.

    B is refused because A already has the project. B is still stamped with
    ownership — it is a worker this run made and has to stay attributable — but
    it acquired nothing. When the caller cleans B up, nothing of A's may be
    released, and a third worker must still be excluded from A's checkout.
    """
    redis = aioredis.FakeRedis(decode_responses=True)
    manager = WorkerManager(redis=redis, docker_client=docker)

    await _create(manager, "worker-a", "run-a")
    assert await redis.sismember("workspace:active_projects", PROJECT)

    with pytest.raises(RuntimeError, match="already has active worker"):
        await _create(manager, "worker-b", "run-b")

    # B kept its ownership — it is attributable — and took no lock.
    meta_b = decode_redis_fields(await redis.hgetall("worker:meta:worker-b"))
    assert meta_b["project_id"] == PROJECT
    assert meta_b["run_id"] == "run-b"
    assert WORKSPACE_LOCK_FIELD not in meta_b

    patcher, runner = _compose_runner_patch()
    with patcher as mock_runner_cls:
        mock_runner_cls.return_value = runner
        await manager.delete_worker("worker-b", reason="timeout")

    # A is untouched: still RUNNING, still the holder, still excluding others.
    status_a = await redis.hget("worker:status:worker-a", "status")
    assert status_a == WorkerStatus.RUNNING
    assert await redis.sismember("workspace:active_projects", PROJECT)
    assert await manager._check_project_lock(PROJECT) == "worker-a"

    with pytest.raises(RuntimeError, match="already has active worker"):
        await _create(manager, "worker-c", "run-c")

    # And B's cleanup did not spend one of A's project's retries either.
    assert await redis.get(f"workspace:{PROJECT}:failure_count") is None


@pytest.mark.asyncio
async def test_a_refused_worker_fails_fast_instead_of_timing_out(docker):
    """A refusal is terminal in Redis, because the caller was already ACKed.

    The create command is answered before the slow work, and the caller then
    polls `worker:status`. A refused worker with no status is one the caller
    waits the full readiness timeout for and then publishes a delete for — which
    is exactly how a refusal turned into someone else's lock being released.
    """
    redis = aioredis.FakeRedis(decode_responses=True)
    manager = WorkerManager(redis=redis, docker_client=docker)

    await _create(manager, "worker-a", "run-a")

    with pytest.raises(RuntimeError, match="already has active worker"):
        await _create(manager, "worker-b", "run-b")

    assert await redis.hget("worker:status:worker-b", "status") == WorkerStatus.FAILED
    assert PROJECT in await redis.get("worker:error:worker-b")


@pytest.mark.asyncio
async def test_deleting_a_worker_that_never_acquired_releases_nothing(docker):
    """The release path reads the holder fact and nothing else.

    A worker carrying full ownership of a project it never acquired — this is
    what every refused worker and every QA executor looks like — must not free
    that project on its way out.
    """
    redis = aioredis.FakeRedis(decode_responses=True)
    await redis.sadd("workspace:active_projects", PROJECT)
    await redis.hset(
        "worker:meta:worker-not-a-holder",
        mapping={
            "worker_type": "developer",
            "project_id": PROJECT,
            "run_id": "run-x",
            "attempt_id": "attempt-x",
        },
    )
    manager = WorkerManager(redis=redis, docker_client=docker)

    patcher, runner = _compose_runner_patch()
    with patcher as mock_runner_cls:
        mock_runner_cls.return_value = runner
        await manager.delete_worker("worker-not-a-holder", reason="timeout")

    assert await redis.sismember("workspace:active_projects", PROJECT)


@pytest.mark.asyncio
async def test_the_holder_is_the_one_that_won_the_acquisition(docker):
    """Two creates that race past the check: only one comes out holding it.

    The `SADD` is the acquisition, so its result is what decides. The loser is
    refused rather than quietly proceeding onto the same checkout, and only the
    winner carries the holder fact.
    """
    redis = aioredis.FakeRedis(decode_responses=True)
    manager = WorkerManager(redis=redis, docker_client=docker)

    await manager._acquire_workspace_lock("worker-a", PROJECT)

    with pytest.raises(RuntimeError, match="taken by a concurrent worker"):
        await manager._acquire_workspace_lock("worker-b", PROJECT)

    meta_a = decode_redis_fields(await redis.hgetall("worker:meta:worker-a"))
    meta_b = decode_redis_fields(await redis.hgetall("worker:meta:worker-b"))
    assert meta_a[WORKSPACE_LOCK_FIELD] == PROJECT
    assert WORKSPACE_LOCK_FIELD not in meta_b


@pytest.mark.asyncio
async def test_a_qa_executor_does_not_release_a_developer_workers_lock(docker):
    """A QA executor owns the project and holds none of its workspace.

    It records the same `project_id` as the developer worker — that is its
    ownership, and the point of the ownership card — so its deletion is the
    other way this could reach for a lock it never took.
    """
    redis = aioredis.FakeRedis(decode_responses=True)
    manager = WorkerManager(redis=redis, docker_client=docker)

    await _create(manager, "worker-a", "run-a")

    await redis.hset(
        "worker:meta:qa-executor",
        mapping={
            "worker_type": "qa",
            "project_id": PROJECT,
            "run_id": "run-a",
            "attempt_id": "qa-attempt",
        },
    )

    with (
        patch("src.manager.workspace_mod.remove_workspace"),
        patch("src.manager.qa_egress.tear_down", new_callable=AsyncMock),
    ):
        await manager.delete_worker("qa-executor", reason="completed")

    assert await redis.sismember("workspace:active_projects", PROJECT)
    assert await manager._check_project_lock(PROJECT) == "worker-a"

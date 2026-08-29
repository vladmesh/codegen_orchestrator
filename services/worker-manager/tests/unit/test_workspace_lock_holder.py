"""The workspace lock belongs to the worker that acquired it, and to no other.

Acquisition decides whether a developer worker exists at all; ownership
describes a worker that does. So ownership is stamped by the acquisition and
nowhere earlier: a worker refused before it could take the project carries no
`project_id`, and cannot be mistaken for the holder of a checkout it never got.
A QA executor is the one worker that owns a project without taking its
workspace, and it is excluded from the mutex by its worker type.

These tests hold that boundary in both directions: a refused worker must release
nothing on its way out, and the sweep that clears stale projects must never be
able to take the workspace away from a worker that is acquiring it.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fakeredis import aioredis

from shared.contracts.dto.worker import WorkerStatus
from shared.contracts.queues.worker import WorkerOwnership
from shared.contracts.vocab import AgentType
from shared.redis import decode_redis_fields
from shared.queues import WORKER_COMMANDS

from src.garbage_collector import garbage_collect_workspaces
from src.manager import WorkerManager


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

    B is refused because A already has the project. Nothing describes a worker
    that was refused: B never reaches the stamp, so it carries no project of
    A's for the release path to hand back, and a third worker must still be
    excluded from A's checkout.
    """
    redis = aioredis.FakeRedis(decode_responses=True)
    manager = WorkerManager(redis=redis, docker_client=docker)

    await _create(manager, "worker-a", "run-a")
    assert await redis.sismember("workspace:active_projects", PROJECT)

    with pytest.raises(RuntimeError, match="already has active worker"):
        await _create(manager, "worker-b", "run-b")

    # B took no project lock or ownership. Its pre-container metadata still
    # records the selected executor and auth mode so diagnostics never infer
    # those facts after a container appears.
    meta_b = decode_redis_fields(await redis.hgetall("worker:meta:worker-b"))
    assert meta_b == {
        "worker_type": "developer",
        "agent_type": "claude",
        "auth_mode": "host_session",
    }

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
    assert await redis.xlen(WORKER_COMMANDS) == 0


@pytest.mark.asyncio
async def test_workspace_lock_refusal_does_not_poison_executor_inventory(docker, monkeypatch):
    """The retained terminal diagnostic record is not a lease needing a container."""
    redis = aioredis.FakeRedis(decode_responses=True)
    manager = WorkerManager(redis=redis, docker_client=docker)
    docker.list_containers = AsyncMock(return_value=[])
    monkeypatch.setattr(manager, "_check_project_lock", AsyncMock(return_value="worker-a"))

    with pytest.raises(RuntimeError, match="already has active worker"):
        await _create(manager, "worker-b", "run-b")

    assert await manager._executor_leases() == {
        AgentType.CLAUDE: 0,
        AgentType.CODEX: 0,
    }


@pytest.mark.asyncio
async def test_the_holder_is_the_one_that_won_the_acquisition(docker):
    """Two creates that race past the check: only one comes out holding it.

    The `SADD` is the acquisition, so its result is what decides. The loser is
    refused rather than quietly proceeding onto the same checkout, and the
    ownership it wrote on the way in is withdrawn with the refusal.
    """
    redis = aioredis.FakeRedis(decode_responses=True)
    manager = WorkerManager(redis=redis, docker_client=docker)

    await manager._acquire_workspace_lock("worker-a", _ownership("run-a", "attempt-a"))

    with pytest.raises(RuntimeError, match="taken by a concurrent worker"):
        await manager._acquire_workspace_lock("worker-b", _ownership("run-b", "attempt-b"))

    meta_a = decode_redis_fields(await redis.hgetall("worker:meta:worker-a"))
    meta_b = decode_redis_fields(await redis.hgetall("worker:meta:worker-b"))
    assert meta_a["project_id"] == PROJECT
    assert meta_a["run_id"] == "run-a"
    assert "project_id" not in meta_b


@pytest.mark.asyncio
async def test_failed_container_removal_keeps_the_owner_fenced_workspace_lock(docker):
    """A delete command is not teardown evidence and cannot free the checkout."""
    redis = aioredis.FakeRedis(decode_responses=True)
    manager = WorkerManager(redis=redis, docker_client=docker)
    await manager._acquire_workspace_lock("worker-a", _ownership("run-a", "attempt-a"))
    docker.remove_container.side_effect = RuntimeError("docker daemon unavailable")

    await manager.delete_worker("worker-a", reason="timeout")

    assert await redis.get(f"workspace:lock:{PROJECT}") == "worker-a"
    with pytest.raises(RuntimeError, match="taken by a concurrent worker"):
        await manager._acquire_workspace_lock("worker-b", _ownership("run-b", "attempt-b"))


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


@pytest.mark.asyncio
async def test_a_finished_qa_executor_is_not_swept_as_the_projects_stale_holder(docker):
    """The stale-project sweep must not mistake a QA executor for the holder.

    A QA executor carries the project as ownership. It is not what keeps the
    workspace claimed, but while a developer worker holds it, the project is
    live either way — and clearing the entry here would be the same release the
    delete path is forbidden from doing.
    """
    redis = aioredis.FakeRedis(decode_responses=True)
    manager = WorkerManager(redis=redis, docker_client=docker)

    await _create(manager, "worker-a", "run-a")
    await redis.hset(
        "worker:meta:qa-executor",
        mapping={"worker_type": "qa", "project_id": PROJECT, "run_id": "run-a", "attempt_id": "qa-attempt"},
    )

    with patch("src.garbage_collector.settings.SCAFFOLDED_WORKSPACE_PATH", "/nonexistent-workspace-root"):
        await garbage_collect_workspaces(redis)

    assert await redis.sismember("workspace:active_projects", PROJECT)
    assert await manager._check_project_lock(PROJECT) == "worker-a"


@pytest.mark.asyncio
async def test_the_stale_sweep_cannot_run_inside_an_acquisition(docker):
    """The sweep and an acquisition interleave at every await, and never collide.

    The sweep is an independent task in this process, so it may run between any
    two of the acquisition's Redis calls. If it could observe the project in
    `workspace:active_projects` before the acquiring worker's metadata, it would
    judge the project stale, clear the entry, and let a second creator onto the
    same persistent checkout. Drive the sweep at each of those points and prove
    the second creator is refused every time.
    """

    def _instrument(redis, sweep_after):
        """Run the sweep as a task right after the Nth write of the create path."""
        state = {"writes": 0, "swept": False}
        original = {"sadd": redis.sadd, "hset": redis.hset}

        async def _maybe_sweep():
            state["writes"] += 1
            if state["writes"] != sweep_after:
                return
            with patch(
                "src.garbage_collector.settings.SCAFFOLDED_WORKSPACE_PATH",
                "/nonexistent-workspace-root",
            ):
                await asyncio.create_task(garbage_collect_workspaces(redis))
            state["swept"] = True

        def _wrap(name):
            async def wrapped(*args, **kwargs):
                result = await original[name](*args, **kwargs)
                await _maybe_sweep()
                return result

            return wrapped

        return state, patch.object(redis, "sadd", _wrap("sadd")), patch.object(redis, "hset", _wrap("hset"))

    # How many points there are to interleave at: every write the create path
    # makes is one, and the acquisition's two are among them.
    counting_redis = aioredis.FakeRedis(decode_responses=True)
    counted, sadd_patch, hset_patch = _instrument(counting_redis, sweep_after=0)
    with sadd_patch, hset_patch:
        await _create(WorkerManager(redis=counting_redis, docker_client=docker), "worker-count", "run-count")
    write_count = counted["writes"]
    assert write_count > 2

    for sweep_after in range(1, write_count + 1):
        redis = aioredis.FakeRedis(decode_responses=True)
        manager = WorkerManager(redis=redis, docker_client=docker)
        state, sadd_patch, hset_patch = _instrument(redis, sweep_after)

        with sadd_patch, hset_patch:
            await _create(manager, "worker-a", "run-a")

        assert state["swept"], f"the sweep never ran for interleaving point {sweep_after}"

        # Whatever the sweep saw, A still holds the project and B is refused.
        assert await redis.sismember("workspace:active_projects", PROJECT), (
            f"the sweep took the workspace from its holder at interleaving point {sweep_after}"
        )
        with pytest.raises(RuntimeError, match="already has active worker"):
            await _create(manager, "worker-b", "run-b")

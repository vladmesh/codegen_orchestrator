"""Ownership is written when a worker is made, on both sides, by one writer.

A worker container is the only thing that outlives the run that made it. Redis
metadata does not: `delete_worker` removes the container first and deletes
`worker:meta:<id>` in its `finally` block, and `_check_project_lock` deletes a
dead worker's record when the next worker for the same project starts. So the
question "whose was this container?" has to be answerable from the container
itself, and the only moment the answer is certainly known is creation.

These tests hold that line at the unit level: what Docker is asked to label, and
what Redis holds before the container exists. The same properties against a real
daemon — a worker that dies inside one poll interval, and a query scoped to one
run — are in `tests/service/test_worker_ownership_labels.py`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fakeredis import aioredis
import pytest

from shared.contracts.queues.worker import WorkerLabel, WorkerOwnership
from shared.redis import decode_redis_fields
from src.manager import QA_WORKER_TYPE, WorkerManager

pytestmark = pytest.mark.asyncio

OWNERSHIP = WorkerOwnership(project_id="proj-alpha", run_id="eng-alpha-1")
OTHER_RUN = WorkerOwnership(project_id="proj-alpha", run_id="eng-alpha-2")


def _docker_mock():
    docker = MagicMock()
    docker.image_exists = AsyncMock(return_value=True)
    docker.get_image_label = AsyncMock(return_value="basehash0001")
    docker.pull_image = AsyncMock()
    docker.build_image = AsyncMock()
    docker.remove_container = AsyncMock()
    docker.create_network = AsyncMock()
    docker.connect_network = AsyncMock()
    docker.remove_network = AsyncMock()
    docker.exec_in_container = AsyncMock(return_value=(0, ""))
    docker.get_container_logs = AsyncMock(return_value="")
    container = MagicMock()
    container.id = "container-1"
    docker.run_container = AsyncMock(return_value=container)
    return docker


async def test_the_container_is_labelled_with_its_project_and_run():
    """Both facts are on the container, next to the id and type that were there."""
    redis = aioredis.FakeRedis(decode_responses=True)
    docker = _docker_mock()
    manager = WorkerManager(redis=redis, docker_client=docker)

    await manager.create_worker(
        "w-labelled",
        "worker:latest",
        ownership=OWNERSHIP,
        network_name="codegen_worker",
        create_dev_network=False,
    )

    labels = docker.run_container.await_args.kwargs["labels"]
    assert labels[WorkerLabel.PROJECT.value] == "proj-alpha"
    assert labels[WorkerLabel.RUN.value] == "eng-alpha-1"
    assert labels[WorkerLabel.ID.value] == "w-labelled"
    assert labels[WorkerLabel.TYPE.value] == "worker"


async def test_ownership_is_in_redis_before_the_container_is_asked_for():
    """The container cannot exit before a record of it exists.

    `run_container` is what starts something that can die, so the assertion is
    ordering: by the time Docker is called, Redis already answers.
    """
    redis = aioredis.FakeRedis(decode_responses=True)
    docker = _docker_mock()
    seen: dict = {}

    async def record_then_run(**kwargs):
        seen["meta"] = decode_redis_fields(await redis.hgetall("worker:meta:w-ordered"))
        container = MagicMock()
        container.id = "container-1"
        return container

    docker.run_container = AsyncMock(side_effect=record_then_run)
    manager = WorkerManager(redis=redis, docker_client=docker)

    await manager.create_worker(
        "w-ordered",
        "worker:latest",
        ownership=OWNERSHIP,
        network_name="codegen_worker",
        create_dev_network=False,
    )

    assert seen["meta"]["project_id"] == "proj-alpha"
    assert seen["meta"]["run_id"] == "eng-alpha-1"


async def test_a_worker_that_never_reached_a_container_is_still_owned():
    """Creation can fail anywhere; what it made is still this run's to find."""
    redis = aioredis.FakeRedis(decode_responses=True)
    docker = _docker_mock()
    docker.run_container = AsyncMock(side_effect=RuntimeError("no daemon"))
    manager = WorkerManager(redis=redis, docker_client=docker)

    with pytest.raises(RuntimeError):
        await manager.create_worker(
            "w-doomed",
            "worker:latest",
            ownership=OWNERSHIP,
            network_name="codegen_worker",
            create_dev_network=False,
        )

    meta = decode_redis_fields(await redis.hgetall("worker:meta:w-doomed"))
    assert meta["project_id"] == "proj-alpha"
    assert meta["run_id"] == "eng-alpha-1"


async def test_two_runs_of_one_project_are_told_apart_by_their_run_label():
    """A query scoped to one run must not select the neighbouring run's worker."""
    redis = aioredis.FakeRedis(decode_responses=True)
    docker = _docker_mock()
    manager = WorkerManager(redis=redis, docker_client=docker)

    for worker_id, ownership in (("w-run-1", OWNERSHIP), ("w-run-2", OTHER_RUN)):
        await manager.create_worker(
            worker_id,
            "worker:latest",
            ownership=ownership,
            network_name="codegen_worker",
            create_dev_network=False,
        )

    by_run = {
        call.kwargs["labels"][WorkerLabel.RUN.value]: call.kwargs["name"]
        for call in docker.run_container.await_args_list
    }
    assert by_run == {"eng-alpha-1": "worker-w-run-1", "eng-alpha-2": "worker-w-run-2"}


@patch("src.manager.workspace_mod.get_scaffolded_workspace", return_value=(Path("/data/ws/repo-1"), True))
async def test_a_developer_worker_carries_the_ownership_the_request_named(_workspace):
    """The whole path: create request → labels and metadata, one value each."""
    redis = aioredis.FakeRedis(decode_responses=True)
    docker = _docker_mock()
    manager = WorkerManager(redis=redis, docker_client=docker)

    with patch.object(manager, "ensure_or_build_image", new_callable=AsyncMock, return_value="w:latest"):
        await manager.create_worker_with_capabilities(
            worker_id="dev-owned",
            capabilities=[],
            base_image="worker-base:latest",
            ownership=OWNERSHIP,
            repo_id="repo-1",
        )

    labels = docker.run_container.await_args.kwargs["labels"]
    assert labels[WorkerLabel.PROJECT.value] == "proj-alpha"
    assert labels[WorkerLabel.RUN.value] == "eng-alpha-1"
    meta = decode_redis_fields(await redis.hgetall("worker:meta:dev-owned"))
    assert (meta["project_id"], meta["run_id"]) == ("proj-alpha", "eng-alpha-1")


async def test_a_qa_executor_owns_a_project_without_taking_its_workspace_lock(tmp_path):
    """A QA executor is ownable by the same labels, and locks nothing.

    It records the project it is testing, which is what makes it attributable —
    but the workspace mutex belongs to the developer worker that has a checkout.
    A QA run must not block, or release, that.
    """
    redis = aioredis.FakeRedis(decode_responses=True)
    docker = _docker_mock()
    docker.inspect_container = AsyncMock(return_value={"NetworkSettings": {"Networks": {"codegen_qa_egress": {}}}})
    docker.inspect_network = AsyncMock(return_value={"Internal": True})
    manager = WorkerManager(redis=redis, docker_client=docker)
    qa_ownership = WorkerOwnership(project_id="proj-alpha", run_id="qa-alpha-9")

    with (
        patch("src.manager.settings") as settings,
        patch.object(manager, "ensure_or_build_image", new_callable=AsyncMock, return_value="w:latest"),
        patch("src.manager.qa_egress.establish", new_callable=AsyncMock) as establish,
        patch("src.manager.qa_egress.verify_isolation"),
    ):
        settings.ENVIRONMENT = "production"
        settings.DOCKER_NETWORK = ""
        settings.WORKER_NETWORK = "codegen_worker"
        settings.QA_EGRESS_NETWORK = "codegen_qa_egress"
        settings.QA_CLAUDE_BACKEND_HOSTS = ""
        settings.QA_CODEX_BACKEND_HOSTS = ""
        settings.SCAFFOLDED_WORKSPACE_PATH = str(tmp_path)
        settings.WORKER_BROKER_URL = "http://worker-broker:8001"
        settings.WORKER_SUBPROCESS_TIMEOUT_SECONDS = 300
        settings.WORKER_IMAGE_PREFIX = "worker"
        settings.WORKER_DOCKER_LABELS = "{}"
        settings.WORKER_TRANSCRIPT_STORAGE_PATH = str(tmp_path / "transcripts")
        settings.WORKER_TRANSCRIPT_MAX_BYTES = 1024
        settings.WORKER_TRANSCRIPT_RETENTION_DAYS = 1
        establish.return_value = MagicMock(env_vars={})

        await manager.create_worker_with_capabilities(
            worker_id="qa-owned",
            capabilities=[],
            base_image="worker-base:latest",
            ownership=qa_ownership,
            worker_type=QA_WORKER_TYPE,
            instructions="# QA executor",
            task_content="test it",
        )

    labels = docker.run_container.await_args.kwargs["labels"]
    assert labels[WorkerLabel.PROJECT.value] == "proj-alpha"
    assert labels[WorkerLabel.RUN.value] == "qa-alpha-9"
    meta = decode_redis_fields(await redis.hgetall("worker:meta:qa-owned"))
    assert (meta["project_id"], meta["run_id"]) == ("proj-alpha", "qa-alpha-9")
    # The lock is untouched: no membership taken, and a developer worker for the
    # same project is still free to start.
    assert not await redis.sismember("workspace:active_projects", "proj-alpha")
    assert await manager._check_project_lock("proj-alpha") is None
    # The run's egress proxy is labelled with the run that opened it.
    assert establish.await_args.kwargs["labels"][WorkerLabel.RUN.value] == "qa-alpha-9"


async def test_deleting_a_qa_executor_does_not_release_a_developers_workspace(tmp_path):
    """The QA executor records a project it never locked; deleting it frees nothing."""
    redis = aioredis.FakeRedis(decode_responses=True)
    docker = _docker_mock()
    manager = WorkerManager(redis=redis, docker_client=docker)

    await redis.sadd("workspace:active_projects", "proj-alpha")
    await redis.hset(
        "worker:meta:qa-1",
        mapping={"worker_type": QA_WORKER_TYPE, "project_id": "proj-alpha", "run_id": "qa-alpha-9"},
    )

    with (
        patch("src.manager.settings") as settings,
        patch("src.manager.workspace_mod.remove_workspace"),
        patch("src.manager.qa_egress.tear_down", new_callable=AsyncMock),
    ):
        settings.WORKER_IMAGE_PREFIX = "worker"
        settings.SCAFFOLDED_WORKSPACE_PATH = str(tmp_path)
        await manager.delete_worker("qa-1", reason="completed")

    assert await redis.sismember("workspace:active_projects", "proj-alpha")

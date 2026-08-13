"""A worker that lived and died between two polls is still attributable.

Against a real Docker daemon and the real Redis, through the real
`WorkerManager.create_worker`. Nothing here samples a worker while it is alive:
each one is started with an environment that makes it exit at once, the test
blocks on its exit, its Redis metadata is deleted the way `delete_worker` deletes
it, and only then is anything asked about it — by label, from
`docker ps -a`, which is `containers.list(all=True, filters={"label": ...})`,
the same daemon endpoint the CLI calls.

That is the case the previous attempt (`codegen-orchestrator-1181`) could not
cover: a five-second poll cannot see a container that lived for one second, so
ownership cannot be observed afterwards. It has to have been written at
creation, which is what these tests hold.

The exited containers are left in place until teardown on purpose — a removed
container is gone from `docker ps -a` too, and what is being proved is that the
labels survive the worker's death, not the container's deletion.
"""

from __future__ import annotations

import asyncio
import os
import secrets

import docker
import pytest
from redis.asyncio import Redis

from shared.contracts.queues.worker import WorkerLabel, WorkerOwnership
from src.manager import WorkerManager

# Any image the stack has built. The worker's real agent image is far more
# expensive to produce and carries nothing this test depends on: what is under
# test is what worker-manager stamps on a container, not what runs inside it.
TEST_IMAGE = os.environ.get("QA_EGRESS_TEST_IMAGE", "codegen-orchestrator/worker-manager:test")
REDIS_URL = os.environ["REDIS_URL"]

# The image's own entrypoint reaches Redis while it starts. Point it at a closed
# port and the container dies in about a second, on its own, with a real exit
# code — the worker that "was created and destroyed inside one poll interval".
DIES_IMMEDIATELY = {"REDIS_URL": "redis://127.0.0.1:1/0"}

EXIT_TIMEOUT_SECONDS = 60


@pytest.fixture(scope="module")
def daemon():
    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:  # noqa: BLE001 — an ownership test without Docker proves nothing
        pytest.skip(f"no Docker daemon available: {exc}")
    try:
        client.images.get(TEST_IMAGE)
    except docker.errors.ImageNotFound:
        pytest.skip(f"{TEST_IMAGE} is not built here")
    return client


@pytest.fixture(scope="module")
def network(daemon):
    net = daemon.networks.create(f"ownership-{secrets.token_hex(4)}", driver="bridge")
    try:
        yield net
    finally:
        try:
            net.remove()
        except Exception:  # noqa: BLE001, S110 — teardown of a test fixture
            pass


@pytest.fixture
def dead_worker(daemon, network):
    """Create workers that die at once, and clean them up afterwards.

    Returns a callable that hands back the worker id and the container id. It
    deliberately returns nothing about the container's identity beyond that: a
    test that read the labels here would be sampling the worker, which is the
    thing this file must not do.
    """
    created: list[str] = []

    async def _create_async(ownership: WorkerOwnership) -> tuple[str, str]:
        worker_id = f"own-{secrets.token_hex(4)}"
        redis = Redis.from_url(REDIS_URL, decode_responses=True)
        try:
            manager = WorkerManager(redis=redis)
            container_id = await manager.create_worker(
                worker_id,
                TEST_IMAGE,
                ownership=ownership,
                env_vars=dict(DIES_IMMEDIATELY),
                network_name=network.name,
                create_dev_network=False,
            )
            created.append(container_id)
            # Block on the exit rather than polling for it: a poll is a sample,
            # and the worker may already be gone before the first one lands.
            daemon.api.wait(container_id, timeout=EXIT_TIMEOUT_SECONDS)
            # Everything Redis knew about this worker, deleted exactly as
            # `delete_worker` deletes it in its `finally` block.
            await redis.delete(
                f"worker:status:{worker_id}",
                f"worker:meta:{worker_id}",
                f"worker:error:{worker_id}",
                f"worker:broker:{worker_id}",
                f"worker:last_activity:{worker_id}",
            )
            assert await redis.hgetall(f"worker:meta:{worker_id}") == {}
            return worker_id, container_id
        finally:
            await redis.aclose()

    def _create(ownership: WorkerOwnership) -> tuple[str, str]:
        # Driven synchronously: the service runner has no asyncio mode
        # configured, and the only async parts here are Redis and the manager.
        return asyncio.run(_create_async(ownership))

    try:
        yield _create
    finally:
        for container_id in created:
            try:
                daemon.containers.get(container_id).remove(force=True)
            except Exception:  # noqa: BLE001, S110 — teardown of a test fixture
                pass


def _by_labels(daemon, **labels) -> list:
    """`docker ps -a --filter label=k=v ...`, over the same daemon endpoint."""
    return daemon.containers.list(all=True, filters={"label": [f"{key}={value}" for key, value in labels.items()]})


def test_a_worker_that_died_unsampled_is_still_attributable(daemon, dead_worker):
    """Created, dead, Redis gone — and the container still says whose it was."""
    ownership = WorkerOwnership(project_id=f"proj-{secrets.token_hex(3)}", run_id=f"run-{secrets.token_hex(3)}")

    worker_id, container_id = dead_worker(ownership)

    found = _by_labels(
        daemon,
        **{
            WorkerLabel.PROJECT.value: ownership.project_id,
            WorkerLabel.RUN.value: ownership.run_id,
        },
    )
    assert [container.id for container in found] == [container_id]

    container = found[0]
    assert container.labels[WorkerLabel.ID.value] == worker_id
    assert container.labels[WorkerLabel.TYPE.value] == "worker"
    assert container.labels[WorkerLabel.PROJECT.value] == ownership.project_id
    assert container.labels[WorkerLabel.RUN.value] == ownership.run_id
    # It really is dead, and the record of its death is readable from here.
    state = daemon.api.inspect_container(container_id)["State"]
    assert state["Status"] == "exited"
    assert state["ExitCode"] != 0


def test_a_query_scoped_to_one_run_never_selects_a_neighbouring_run(daemon, dead_worker):
    """Two runs of the same project, and neither answers the other's question."""
    project = f"proj-{secrets.token_hex(3)}"
    first = WorkerOwnership(project_id=project, run_id=f"run-a-{secrets.token_hex(3)}")
    second = WorkerOwnership(project_id=project, run_id=f"run-b-{secrets.token_hex(3)}")

    _, first_container = dead_worker(first)
    _, second_container = dead_worker(second)

    assert [c.id for c in _by_labels(daemon, **{WorkerLabel.RUN.value: first.run_id})] == [first_container]
    assert [c.id for c in _by_labels(daemon, **{WorkerLabel.RUN.value: second.run_id})] == [second_container]
    # The project they share does select both — ownership is two facts, and the
    # coarser one is a coarser question, not a wrong answer.
    assert {c.id for c in _by_labels(daemon, **{WorkerLabel.PROJECT.value: project})} == {
        first_container,
        second_container,
    }


def test_a_run_scoped_query_selects_no_long_lived_service_container(daemon, dead_worker):
    """The stack's own containers are on this daemon and answer to no run.

    Worth stating explicitly: the cleanup this makes possible acts on whatever a
    run-scoped query returns, and a query that swept up `worker-manager` or
    `redis` would take the platform down with the run.
    """
    ownership = WorkerOwnership(project_id=f"proj-{secrets.token_hex(3)}", run_id=f"run-{secrets.token_hex(3)}")

    _, container_id = dead_worker(ownership)

    everything = daemon.containers.list(all=True)
    assert len(everything) > 1, "this daemon runs the service stack; the test is meaningless without it"

    selected = _by_labels(daemon, **{WorkerLabel.RUN.value: ownership.run_id})
    assert [container.id for container in selected] == [container_id]
    for container in everything:
        if container.id == container_id:
            continue
        assert container.labels.get(WorkerLabel.RUN.value) != ownership.run_id

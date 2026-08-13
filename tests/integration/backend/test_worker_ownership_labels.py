"""A worker that lived and died between two polls is still attributable.

Through the real path — a `CreateWorkerCommand` on `worker:commands`, the real
worker-manager, a real container on the Docker-in-Docker daemon — and then
against the labels alone.

No attribution fact is read while a worker is alive. The only thing that happens
before a worker dies here is worker-manager's own create handshake; the test then
destroys the container (a crash, from the outside, inside what would be one
harness poll), deletes the worker's Redis keys exactly as `delete_worker` deletes
them in its `finally` block, and only afterwards asks Docker who owned it.

That is the case the previous attempt (`codegen-orchestrator-1181`) could not
cover: a five-second poll cannot see a container that lived for one second, so
ownership cannot be observed after the fact. It has to have been written at
creation.

The dead containers are deliberately left in place until the stack is torn down:
a removed container is gone from `docker ps -a` too, and what is proved here is
that ownership survives the worker's death — not the container's deletion.
"""

from uuid import uuid4

import pytest

from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    WorkerConfig,
    WorkerLabel,
    WorkerOwnership,
)

from .conftest import (
    REDIS_STREAM_COMMANDS,
    REDIS_STREAM_DEV_RESPONSES,
    wait_for_create_response,
)

# The Redis keys `delete_worker` removes once a worker is gone. Deleting them by
# hand here is the point: the attribution below must not need any of them.
WORKER_KEY_PATTERNS = (
    "worker:status:{worker_id}",
    "worker:meta:{worker_id}",
    "worker:error:{worker_id}",
    "worker:broker:{worker_id}",
    "worker:last_activity:{worker_id}",
)


async def _dead_owned_worker(redis_client, docker_client, repo_id: str, ownership: WorkerOwnership):
    """Create one worker for this owner, kill it, and forget it in Redis.

    Returns the worker id and the id of its container. Nothing about the
    container's ownership is read here — that is what the tests do, after it is
    dead.
    """
    request_id = f"own-{uuid4().hex[:8]}"
    command = CreateWorkerCommand(
        request_id=request_id,
        config=WorkerConfig(
            name=f"dev-own-{uuid4().hex[:8]}",
            worker_type="developer",
            agent_type=AgentType.CLAUDE,
            instructions="Ownership fixture. This worker is created to die.",
            allowed_commands=["*"],
            capabilities=[],
            ownership=ownership,
            # api_key mode keeps the container off the host session mount: this
            # worker never runs an agent, it only has to be created and die.
            auth_mode="api_key",
            repo_id=repo_id,
        ),
    )
    await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": command.model_dump_json()})

    result = await wait_for_create_response(
        redis_client, REDIS_STREAM_DEV_RESPONSES, request_id=request_id
    )
    assert result.success is True, f"Worker creation failed: {result.error}"
    worker_id = result.worker_id

    # The whole life of this worker: created, and gone again well inside one
    # five-second poll. `kill` stands in for the crash; nothing has looked at
    # what the container carries.
    container = docker_client.containers.get(f"worker-{worker_id}")
    container_id = container.id
    container.kill()

    await redis_client.delete(*[key.format(worker_id=worker_id) for key in WORKER_KEY_PATTERNS])
    assert await redis_client.hgetall(f"worker:meta:{worker_id}") == {}

    return worker_id, container_id


def _by_labels(docker_client, **labels):
    """`docker ps -a --filter label=k=v …`, over the daemon endpoint the CLI calls."""
    return docker_client.containers.list(
        all=True, filters={"label": [f"{key}={value}" for key, value in labels.items()]}
    )


def _fresh_ownership() -> WorkerOwnership:
    token = uuid4().hex[:8]
    return WorkerOwnership(
        project_id=f"proj-{token}", run_id=f"live-{token}", attempt_id=f"eng-{token}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
class TestOwnershipSurvivesTheWorker:
    async def test_a_dead_unsampled_worker_is_still_attributable(
        self, redis_client, docker_client, scaffolded_workspace
    ):
        """Dead, forgotten by Redis — and the container still says whose it was."""
        ownership = _fresh_ownership()

        worker_id, container_id = await _dead_owned_worker(
            redis_client, docker_client, scaffolded_workspace, ownership
        )

        found = _by_labels(
            docker_client,
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
        # It really is dead, and Docker still holds the record of how it ended.
        container.reload()
        assert container.attrs["State"]["Status"] == "exited"

    async def test_a_query_scoped_to_one_run_never_selects_a_neighbouring_run(
        self, redis_client, docker_client, scaffolded_workspace
    ):
        """Two runs, and neither one answers the other's question."""
        first = _fresh_ownership()
        second = _fresh_ownership()

        _, first_container = await _dead_owned_worker(
            redis_client, docker_client, scaffolded_workspace, first
        )
        _, second_container = await _dead_owned_worker(
            redis_client, docker_client, scaffolded_workspace, second
        )

        assert [
            c.id for c in _by_labels(docker_client, **{WorkerLabel.RUN.value: first.run_id})
        ] == [first_container]
        assert [
            c.id for c in _by_labels(docker_client, **{WorkerLabel.RUN.value: second.run_id})
        ] == [second_container]
        assert [
            c.id for c in _by_labels(docker_client, **{WorkerLabel.PROJECT.value: first.project_id})
        ] == [first_container]

    async def test_a_run_scoped_query_selects_nothing_else_on_the_daemon(
        self, redis_client, docker_client, scaffolded_workspace
    ):
        """Every other container on this daemon answers to no run.

        Worth stating explicitly: the cleanup this ownership makes possible acts
        on whatever a run-scoped query returns, and a query that swept up a
        neighbouring worker — or something that is not a worker at all — would
        take more than its own run down with it.
        """
        ownership = _fresh_ownership()

        _, container_id = await _dead_owned_worker(
            redis_client, docker_client, scaffolded_workspace, ownership
        )

        everything = docker_client.containers.list(all=True)
        assert len(everything) > 1, "the daemon has only this worker; the check would be vacuous"

        selected = _by_labels(docker_client, **{WorkerLabel.RUN.value: ownership.run_id})
        assert [container.id for container in selected] == [container_id]
        for container in everything:
            if container.id == container_id:
                continue
            assert container.labels.get(WorkerLabel.RUN.value) != ownership.run_id

"""The run that started the work is the run on the container.

This is the test the previous round did not have. `test_worker_ownership_labels`
proves that whatever ownership a create request carries survives the worker's
death — but it supplies that ownership itself, so it cannot see a pipeline that
faithfully stamps the *wrong* identity. Here nothing is supplied: a run id is
minted the way a live run mints it (an `OwnershipManifest`), handed to the
platform once, at project creation, and then never mentioned again. Every later
value is read back out of the real system:

    manifest.run_id
      → POST /api/projects/          (the one place a run enters the system)
      → the project row
      → POST /api/tasks/{id}/spawn-worker
      → the EngineeringMessage on `engineering:queue`   (read back off Redis)
      → WorkerOwnership.for_engineering(msg)            (production constructor)
      → CreateWorkerCommand → worker-manager → a real container
      → docker ps -a --filter label=com.codegen.run.id=<manifest.run_id>

The last step is the assertion that matters: a query scoped to exactly the id
the run was born with selects the worker that run caused, after that worker has
died and its Redis record has been deleted.

The engineering subgraph itself is not driven here — it reaches GitHub, which
this stack deliberately has no credentials for — so the create command is issued
from the message the API really published, with the ownership the consumer
really derives. Everything about the *identity* is the platform's own; nothing
about it is the test's.
"""

import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import pytest

from shared.contracts.queues.engineering import EngineeringMessage
from shared.contracts.queues.worker import WorkerLabel, WorkerOwnership

from .test_worker_ownership_labels import _by_labels, _dead_owned_worker

ENGINEERING_QUEUE = "engineering:queue"


def _ownership_manifest(run_id: str):
    """The live harness's own manifest, loaded from `tests/live`.

    Imported by path: `tests/live` is not a package, and the point of using the
    real class is that this test asks Docker for exactly the identity a live run
    would own — not for a look-alike string invented here.
    """
    module_path = Path(__file__).resolve().parents[2] / "live" / "live_harness.py"
    spec = importlib.util.spec_from_file_location("live_harness_for_ownership", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.OwnershipManifest(run_id=run_id)


async def _published_engineering_message(redis_client, *, run_row_id: str) -> EngineeringMessage:
    """The message the API really published, read back off the queue.

    The engineering consumer in this stack reads the same stream through its
    group; entries stay in the stream either way, so this reads the bytes that
    were published rather than anything this test constructed.
    """
    for _, fields in await redis_client.xrange(ENGINEERING_QUEUE):
        data = fields.get("data")
        if not data:
            continue
        payload = json.loads(data)
        if payload.get("task_id") == run_row_id:
            return EngineeringMessage.model_validate(payload)
    raise AssertionError(f"no engineering message for run {run_row_id} on {ENGINEERING_QUEUE}")


@pytest.mark.integration
@pytest.mark.asyncio
class TestTheInitiatingRunReachesTheContainer:
    async def test_a_worker_is_attributable_by_the_run_id_its_run_was_born_with(
        self, api_client, redis_client, docker_client, seed_project, seed_task, scaffolded_workspace
    ):
        """Query Docker by exactly `manifest.run_id` and find the run's worker."""
        manifest = _ownership_manifest(f"live-{uuid4().hex[:12]}")

        # The one place the run enters the system.
        project = await seed_project(
            name=f"ownership-{uuid4().hex[:6]}",
            status="active",
            # spawn-worker passes the engineering dispatch admission point, and
            # a project without a prepared workspace is refused there.
            config={"workspace_ready": True},
            initiating_run_id=manifest.run_id,
        )
        manifest.own("project", project["id"])
        assert project["initiating_run_id"] == manifest.run_id
        # The run is its own identity, not a second name for the project.
        assert manifest.run_id != project["id"]

        task = await seed_task(title="Ownership propagation", project_id=project["id"])
        spawn = await api_client.post(f"/api/tasks/{task['id']}/spawn-worker", json={})
        assert spawn.status_code == 200, spawn.text
        run_row_id = spawn.json()["run"]["id"]

        msg = await _published_engineering_message(redis_client, run_row_id=run_row_id)

        # What the pipeline carries: the initiating run, distinct from both the
        # engineering attempt it created and the project it is working on.
        assert msg.initiating_run_id == manifest.run_id
        assert msg.task_id == run_row_id
        assert msg.task_id != manifest.run_id
        assert msg.project_id == project["id"]

        # The production constructor — the only place a developer worker's
        # ownership is derived — applied to that message.
        ownership = WorkerOwnership.for_engineering(msg)
        assert ownership == WorkerOwnership(
            project_id=project["id"], run_id=manifest.run_id, attempt_id=run_row_id
        )

        worker_id, container_id = await _dead_owned_worker(
            redis_client, docker_client, scaffolded_workspace, ownership
        )

        # Dead, forgotten by Redis — and the run that started all this can still
        # find it, by the id it had before the project existed.
        found = _by_labels(docker_client, **{WorkerLabel.RUN.value: manifest.run_id})
        assert [container.id for container in found] == [container_id]

        labels = found[0].labels
        assert labels[WorkerLabel.ID.value] == worker_id
        assert labels[WorkerLabel.PROJECT.value] == project["id"]
        assert labels[WorkerLabel.ATTEMPT.value] == run_row_id
        # The attempt is carried, and it is carried separately: a query for the
        # run must not have to know which attempt produced the worker, and one
        # for the attempt must not answer for the whole run.
        assert labels[WorkerLabel.ATTEMPT.value] != labels[WorkerLabel.RUN.value]
        assert not _by_labels(docker_client, **{WorkerLabel.RUN.value: run_row_id})

        # And nothing else on this daemon answers to that run — not the
        # long-lived services, not a neighbouring run's worker.
        for container in docker_client.containers.list(all=True):
            if container.id == container_id:
                continue
            assert container.labels.get(WorkerLabel.RUN.value) != manifest.run_id

        assert await redis_client.hgetall(f"worker:meta:{worker_id}") == {}

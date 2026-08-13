"""The run's artifact accounts for a worker the run never saw alive.

`test_worker_ownership_labels` proves the labels survive the worker. This proves
the evidence does: the same dead, Redis-forgotten worker is handed to the live
harness's own collector (`tests/live/run_evidence.py`), which is given nothing
but the run id and a docker daemon, and the artifact comes back carrying that
worker's exit code, a bounded log tail and where its transcript was retained.

Nothing is sampled while the worker lives. Each test creates a worker through
the real path — a `CreateWorkerCommand` on `worker:commands`, the real
worker-manager, a real container on the DinD daemon — kills it, deletes the
Redis keys `delete_worker` deletes, and only then builds the collector. A
collector that had to catch a container alive would have nothing to report here,
which is exactly what the previous attempt (codegen-orchestrator-1181) could not
get past.

The one thing a label cannot survive is the removal of the container itself:
`docker ps -a` forgets a removed container, and `delete_worker` removes rather
than stops. So the run does not race the deleter — the deleter captures. One
test here runs the whole ordinary delete path through the real worker-manager
before anything observes the worker at all, and the artifact still comes back
with that worker's exit code and log tail: not a miss, and not an omission.

What is left when even that fails — no container, no record — is proved too: it
comes back as an explicit missed capture naming the reason, never as an
omission, because a missing worker record reads as "nothing ran".
"""

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

import pytest

from shared.contracts.queues.worker import (
    DeleteWorkerCommand,
    DeleteWorkerResponse,
    WorkerLabel,
)
from shared.contracts.worker_evidence import removed_worker_evidence_key

from .conftest import REDIS_STREAM_COMMANDS, REDIS_STREAM_DEV_RESPONSES, REDIS_URL
from .test_worker_ownership_labels import (
    _by_labels,
    _dead_owned_worker,
    _fresh_ownership,
    _owned_worker,
)

LIVE_TESTS = Path(__file__).resolve().parents[2] / "live"


def _run_evidence():
    """The live harness's collector, imported from `tests/live` by path.

    `tests/live` is not a package, and the point of this test is that the module
    the production matrix runs is the module answering the label query here —
    not a re-implementation of it that could drift.
    """
    if str(LIVE_TESTS) not in sys.path:
        sys.path.insert(0, str(LIVE_TESTS))
    spec = importlib.util.spec_from_file_location(
        "run_evidence_for_integration", LIVE_TESTS / "run_evidence.py"
    )
    module = importlib.util.module_from_spec(spec)
    # The module must be in sys.modules *before* it executes: it declares
    # dataclasses under `from __future__ import annotations`, and @dataclass
    # resolves those string annotations through sys.modules[cls.__module__].
    # Unregistered, that lookup is None and the class body raises
    # AttributeError from dataclasses itself.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def _delete_through_worker_manager(redis_client, worker_id: str, timeout: int = 180) -> None:
    """Run the ordinary delete path to completion and wait for its response.

    No shortcut: the command goes on `worker:commands` and the real
    worker-manager consumes it, so whatever `delete_worker` does for a worker in
    production — including capturing its ending before removing its container —
    is what happens here.
    """
    request_id = f"del-{uuid4().hex[:8]}"
    await redis_client.xadd(
        REDIS_STREAM_COMMANDS,
        {
            "data": DeleteWorkerCommand(
                request_id=request_id, worker_id=worker_id, reason="failed"
            ).model_dump_json()
        },
    )
    deadline = time.time() + timeout
    cursor = "0"
    while time.time() < deadline:
        messages = await redis_client.xread(
            {REDIS_STREAM_DEV_RESPONSES: cursor}, count=10, block=1000
        )
        for _stream, entries in messages or []:
            for message_id, fields in entries:
                cursor = message_id
                payload = json.loads(fields["data"])
                if payload.get("request_id") != request_id:
                    continue
                response = DeleteWorkerResponse.model_validate(payload)
                assert response.success is True, f"delete failed: {response.error}"
                return
        await asyncio.sleep(0.1)
    raise TimeoutError(f"worker-manager did not answer the delete of {worker_id}")


def _artifact_for(run_evidence, docker_client, ownership, tmp_path, **collector_kwargs) -> dict:
    """Build one artifact for a run, reading only the daemon and Redis."""
    collector = run_evidence.RunEvidenceCollector(
        run_id=ownership.run_id,
        probe=run_evidence.docker_sdk_probe(
            docker_client, run_evidence.redis_removed_workers(REDIS_URL)
        ),
        **collector_kwargs,
    )
    collector.capture()
    return run_evidence.build_artifact(
        {
            "project_id": ownership.project_id,
            "agent_type": "claude",
            "qa_requires_executor": False,
            "run_evidence": collector,
        },
        root=tmp_path,
    )


@pytest.mark.integration
@pytest.mark.asyncio
class TestEvidenceFollowsTheRunLabel:
    async def test_a_worker_killed_and_forgotten_is_still_fully_attributed(
        self, redis_client, docker_client, scaffolded_workspace, tmp_path
    ):
        """Dead, deleted from Redis — and its exit is still in the artifact."""
        run_evidence = _run_evidence()
        ownership = _fresh_ownership()

        worker_id, container_id = await _dead_owned_worker(
            redis_client, docker_client, scaffolded_workspace, ownership
        )
        # Nothing has read this container yet, and it is already dead: whatever
        # the artifact says next was read from a corpse the label pointed at.
        container = docker_client.containers.get(container_id)
        container.reload()
        assert container.attrs["State"]["Status"] == "exited"
        assert await redis_client.hgetall(f"worker:meta:{worker_id}") == {}

        artifact = _artifact_for(run_evidence, docker_client, ownership, tmp_path)

        assert [worker["worker_id"] for worker in artifact["workers"]] == [worker_id]
        worker = artifact["workers"][0]
        assert worker["discovered_by"] == run_evidence.Discovery.RUN_LABEL.value
        assert worker["role"] == run_evidence.WorkerRole.DEVELOPER.value
        assert worker["ownership_labels"] == {
            WorkerLabel.PROJECT.value: ownership.project_id,
            WorkerLabel.RUN.value: ownership.run_id,
            WorkerLabel.ATTEMPT.value: ownership.attempt_id,
        }
        assert worker["exit_code"]["status"] == run_evidence.CaptureStatus.CAPTURED.value
        assert isinstance(worker["exit_code"]["value"], int)
        assert worker["state"]["value"]["running"] is False
        assert worker["log_tail"]["status"] == run_evidence.CaptureStatus.CAPTURED.value
        assert artifact["discovery"]["run_id"] == ownership.run_id
        assert artifact["capture_errors"] == []

        # Where a Codex exit is attributed afterwards: the transcript directory
        # worker-wrapper writes into the host bind mount. Captured or missed —
        # never an empty field either way.
        for level in worker["transcript"].values():
            assert level["status"] in {status.value for status in run_evidence.CaptureStatus}
            if level["status"] == run_evidence.CaptureStatus.MISSED.value:
                assert level["reason"]
                assert level["value"] is None

    async def test_a_worker_deleted_before_anything_looked_still_carries_its_exit(
        self, redis_client, docker_client, scaffolded_workspace, tmp_path
    ):
        """The sequence a harness can never win, decided at the vanishing point.

        The worker is created, it dies, and the ordinary delete path runs to
        completion — container removed, `worker:meta` deleted — before anything
        observes it. `docker ps -a` has nothing to say about it and neither has
        Redis's worker metadata. The artifact still names it, with the exit code
        and the log tail worker-manager read in the instant before it removed
        the container, because that is where the capture belongs.
        """
        run_evidence = _run_evidence()
        ownership = _fresh_ownership()

        worker_id, container_id = await _owned_worker(
            redis_client, docker_client, scaffolded_workspace, ownership
        )
        await _delete_through_worker_manager(redis_client, worker_id)

        # Genuinely gone, on both of the sources the previous attempt had.
        assert _by_labels(docker_client, **{WorkerLabel.RUN.value: ownership.run_id}) == []
        assert await redis_client.hgetall(f"worker:meta:{worker_id}") == {}

        artifact = _artifact_for(run_evidence, docker_client, ownership, tmp_path)

        assert [worker["worker_id"] for worker in artifact["workers"]] == [worker_id]
        worker = artifact["workers"][0]
        assert worker["discovered_by"] == run_evidence.Discovery.DELETE_CAPTURE.value
        assert worker["role"] == run_evidence.WorkerRole.DEVELOPER.value
        assert worker["role_evidence"] == run_evidence.RoleEvidence.DELETE_RECORD.value
        assert worker["container_present"] is False
        assert worker["delete_reason"] == "failed"
        assert worker["ownership_labels"] == {
            WorkerLabel.PROJECT.value: ownership.project_id,
            WorkerLabel.RUN.value: ownership.run_id,
            WorkerLabel.ATTEMPT.value: ownership.attempt_id,
        }
        # Not a miss, and not an omission: the two things this card exists to
        # rule out for a worker nobody was watching.
        assert worker["exit_code"]["status"] == run_evidence.CaptureStatus.CAPTURED.value
        assert isinstance(worker["exit_code"]["value"], int)
        assert worker["log_tail"]["status"] == run_evidence.CaptureStatus.CAPTURED.value
        assert isinstance(worker["log_tail"]["value"]["text"], str)
        assert worker["agent_type_executed"]["value"] == "claude"
        # And where to attribute a Codex exit afterwards, kept while the mount
        # was still readable.
        assert worker["transcript"]["host_dir"]["value"].endswith(worker_id)
        assert artifact["capture_errors"] == []

        # The record survives the metadata deletion that follows it, which is
        # the whole reason it is not kept in `worker:meta`.
        assert await redis_client.hexists(removed_worker_evidence_key(ownership.run_id), worker_id)

    async def test_a_removed_container_the_run_owned_is_a_stated_missed_capture(
        self, redis_client, docker_client, scaffolded_workspace, tmp_path
    ):
        """The label's one blind spot, and the artifact says so out loud."""
        run_evidence = _run_evidence()
        ownership = _fresh_ownership()

        worker_id, container_id = await _dead_owned_worker(
            redis_client, docker_client, scaffolded_workspace, ownership
        )
        # Removal, not death: what worker-manager's delete does last.
        docker_client.containers.get(container_id).remove(force=True)
        assert _by_labels(docker_client, **{WorkerLabel.RUN.value: ownership.run_id}) == []

        artifact = _artifact_for(
            run_evidence,
            docker_client,
            ownership,
            tmp_path,
            owned_workers=lambda: [worker_id],
        )

        assert [worker["worker_id"] for worker in artifact["workers"]] == [worker_id]
        worker = artifact["workers"][0]
        assert worker["discovered_by"] == run_evidence.Discovery.OWNERSHIP_MANIFEST.value
        assert worker["container_present"] is False
        for field in ("exit_code", "log_tail", "state", "image"):
            assert worker[field]["status"] == run_evidence.CaptureStatus.MISSED.value
            assert worker[field]["value"] is None
            assert "never listed its container" in worker[field]["reason"]

    async def test_a_worker_whose_removal_record_failed_still_reaches_its_artifact(
        self, redis_client, docker_client, scaffolded_workspace, tmp_path
    ):
        """The last durable name is kept when the durable record cannot be written.

        The record's own storage is the half that can fail, and when it does the
        container is removed anyway — cleanup is never wedged by observability.
        What must not also happen is the deletion of `worker:meta:<id>`: with the
        container gone, no removal record and no metadata, nothing left could
        name this worker and the run's artifact would read as if it never ran.

        Nothing is sampled while the worker lives and no worker id is handed to
        the collector: the ownership manifest is read from the metadata
        worker-manager itself declined to delete.
        """
        run_evidence = _run_evidence()
        ownership = _fresh_ownership()

        worker_id, _ = await _owned_worker(
            redis_client, docker_client, scaffolded_workspace, ownership
        )
        # A genuine store failure inside the real worker-manager: the run's
        # evidence key already holds a string, so the record's HSET is refused
        # by Redis itself rather than by a patched client.
        evidence_key = removed_worker_evidence_key(ownership.run_id)
        await redis_client.set(evidence_key, "not a hash")

        await _delete_through_worker_manager(redis_client, worker_id)

        assert _by_labels(docker_client, **{WorkerLabel.RUN.value: ownership.run_id}) == []
        meta = await redis_client.hgetall(f"worker:meta:{worker_id}")
        assert meta["run_id"] == ownership.run_id
        # The poison is removed before the run reads: what the collector must
        # face is an empty record set, which is what the failed store left.
        await redis_client.delete(evidence_key)

        artifact = _artifact_for(
            run_evidence,
            docker_client,
            ownership,
            tmp_path,
            owned_workers=run_evidence.redis_owned_workers(REDIS_URL, ownership.run_id),
        )

        assert [worker["worker_id"] for worker in artifact["workers"]] == [worker_id]
        worker = artifact["workers"][0]
        assert worker["discovered_by"] == run_evidence.Discovery.OWNERSHIP_MANIFEST.value
        assert worker["container_present"] is False
        for field in ("exit_code", "log_tail", "state", "image"):
            assert worker[field]["status"] == run_evidence.CaptureStatus.MISSED.value
            assert worker[field]["value"] is None
            assert worker[field]["reason"]
        assert artifact["capture_errors"] == []

        # Left behind on purpose, and only this: the leaked name a label sweep
        # collects later, with everything else the deletion erases gone.
        assert await redis_client.hgetall(f"worker:status:{worker_id}") == {}
        await redis_client.delete(f"worker:meta:{worker_id}")

    async def test_one_runs_artifact_never_carries_another_runs_worker(
        self, redis_client, docker_client, scaffolded_workspace, tmp_path
    ):
        """Four combinations share one daemon; each answers for itself alone."""
        run_evidence = _run_evidence()
        ownership = _fresh_ownership()
        neighbour = _fresh_ownership()

        worker_id, _ = await _dead_owned_worker(
            redis_client, docker_client, scaffolded_workspace, ownership
        )
        neighbour_worker_id, _ = await _dead_owned_worker(
            redis_client, docker_client, scaffolded_workspace, neighbour
        )

        artifact = _artifact_for(run_evidence, docker_client, ownership, tmp_path)
        neighbour_artifact = _artifact_for(run_evidence, docker_client, neighbour, tmp_path)

        assert [worker["worker_id"] for worker in artifact["workers"]] == [worker_id]
        assert [worker["worker_id"] for worker in neighbour_artifact["workers"]] == [
            neighbour_worker_id
        ]

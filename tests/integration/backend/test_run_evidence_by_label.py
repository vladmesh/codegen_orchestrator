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
`docker ps -a` forgets a removed container. That case is proved too — it comes
back as an explicit missed capture naming the reason, never as an omission,
because a missing worker record reads as "nothing ran".
"""

import importlib.util
from pathlib import Path
import sys

import pytest

from shared.contracts.queues.worker import WorkerLabel

from .test_worker_ownership_labels import _by_labels, _dead_owned_worker, _fresh_ownership

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
    spec.loader.exec_module(module)
    return module


def _artifact_for(run_evidence, docker_client, ownership, tmp_path, **collector_kwargs) -> dict:
    """Build one artifact for a run, reading only the daemon."""
    collector = run_evidence.RunEvidenceCollector(
        run_id=ownership.run_id,
        probe=run_evidence.docker_sdk_probe(docker_client),
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

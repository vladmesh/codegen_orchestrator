from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

from live_harness import OwnershipManifest
import pytest
import run_evidence
from run_evidence import (
    EVIDENCE_KIND,
    EVIDENCE_SCHEMA_VERSION,
    LOG_TAIL_MAX_CHARS,
    Capture,
    CaptureStatus,
    ContainerProbe,
    Discovery,
    FailureKind,
    ListedWorker,
    QAExercise,
    RoleEvidence,
    RunEvidenceCollector,
    TerminalState,
    WorkerRole,
    build_artifact,
    capture_worker,
    classify_outcome,
    combination_label,
    emit_run_evidence,
    evidence_output_directory,
    parse_removed_workers,
    qa_cell,
    redact_log_tail,
    role_from_worker_id,
    write_artifact,
)

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.deploy import DeployOutcome
from shared.contracts.queues.qa import QAOutcome
from shared.contracts.queues.worker import WorkerLabel, WorkerOwnership
from shared.contracts.worker_evidence import RemovalFact, RemovedWorkerEvidence

pytestmark = pytest.mark.needs_no_api_credential

REPO = "live-test-llm-1a2b3c4d"
RUN_ID = "live-1a2b3c4d5e6f"
PROJECT_ID = "11111111-2222-3333-4444-555555555555"
DEV_WORKER_ID = f"dev-{REPO[:20]}-9f8e7d6c"
DEV_CONTAINER = f"worker-{DEV_WORKER_ID}"
QA_WORKER_ID = "qa-abc123abc123"
QA_CONTAINER = f"worker-{QA_WORKER_ID}"
RUN_START = datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)
CODEX_REPOSITORY = "ghcr.io/org/codegen-orchestrator/worker-base-codex"
REMOVED_AT = "2026-08-13T12:00:20+00:00"


class FakeDocker:
    def __init__(
        self, containers: dict[str, dict] | None = None, logs: dict[str, str] | None = None
    ):
        self.containers = containers or {}
        self.logs_by_container = logs or {}
        self.log_reads: list[str] = []
        self.listed_states: list[str] = []
        self.listing_error: str | None = None
        self.removed: dict[str, list[RemovedWorkerEvidence]] = {}
        self.removal_record_error: str | None = None

    def add(self, container: str, payload: dict, log: str) -> None:
        self.containers[container] = payload
        self.logs_by_container[container] = log

    def remove(self, container: str) -> None:
        self.containers.pop(container, None)
        self.logs_by_container.pop(container, None)

    def delete(self, container: str, *, run_id: str = RUN_ID, **overrides) -> None:
        self.removed.setdefault(run_id, []).append(removal_record(container, **overrides))
        self.remove(container)

    def probe(self) -> ContainerProbe:
        def list_run_workers(run_id: str) -> list[ListedWorker]:
            if self.listing_error is not None:
                raise RuntimeError(self.listing_error)
            listed = []
            for name in sorted(self.containers):
                labels = self.containers[name]["Config"]["Labels"]
                if labels.get(WorkerLabel.TYPE.value) != "worker":
                    continue
                if labels.get(WorkerLabel.RUN.value) != run_id:
                    continue
                self.listed_states.append(self.containers[name]["State"]["Status"])
                listed.append(
                    ListedWorker(
                        container=name,
                        worker_id=labels[WorkerLabel.ID.value],
                        ownership={
                            label.value: labels.get(label.value, "")
                            for label in (
                                WorkerLabel.PROJECT,
                                WorkerLabel.RUN,
                                WorkerLabel.ATTEMPT,
                            )
                        },
                    )
                )
            return listed

        def inspect(container: str) -> dict | None:
            return self.containers.get(container)

        def logs(container: str, tail: int) -> str | None:
            if container not in self.containers:
                return None
            self.log_reads.append(container)
            return self.logs_by_container[container]

        def removed_workers(run_id: str) -> list[RemovedWorkerEvidence]:
            if self.removal_record_error is not None:
                raise RuntimeError(self.removal_record_error)
            return parse_removed_workers(
                [record.model_dump_json() for record in self.removed.get(run_id, [])]
            )

        return ContainerProbe(
            list_run_workers=list_run_workers,
            inspect=inspect,
            logs=logs,
            removed_workers=removed_workers,
        )


def removal_record(
    container: str,
    *,
    worker_id: str | None = None,
    ownership: WorkerOwnership | None = None,
    worker_type: str = "developer",
    agent_type: str = "codex",
    exit_code: int | None = 1,
    exit_reason: str = "the container was still running when it was removed",
    log_tail: str | None = "wrapper said something\n",
    transcript_dir: str | None = "/data/worker-transcripts",
    delete_reason: str | None = "failed",
) -> RemovedWorkerEvidence:
    worker_id = worker_id or container.removeprefix("worker-")
    return RemovedWorkerEvidence(
        worker_id=worker_id,
        container=container,
        ownership=ownership
        or WorkerOwnership(project_id=PROJECT_ID, run_id=RUN_ID, attempt_id="task-1"),
        removed_at=REMOVED_AT,
        delete_reason=delete_reason,
        worker_type=RemovalFact.read(worker_type),
        agent_type=RemovalFact.read(agent_type),
        image=RemovalFact.read({"tag": f"worker-base-{agent_type}:latest", "id": "sha256:abc"}),
        state=RemovalFact.read(
            {
                "status": "exited",
                "running": False,
                "oom_killed": False,
                "started_at": "2026-08-13T12:00:05+00:00",
                "finished_at": "2026-08-13T12:00:19+00:00",
                "error": "",
            }
        ),
        exit_code=(
            RemovalFact.read(exit_code)
            if exit_code is not None
            else RemovalFact.missed(exit_reason)
        ),
        log_tail=(
            RemovalFact.read(log_tail)
            if log_tail is not None
            else RemovalFact.missed("the container's log could not be read before removal")
        ),
        transcript_dir=(
            RemovalFact.read(f"{transcript_dir}/{worker_id}")
            if transcript_dir is not None
            else RemovalFact.missed("the container declares no transcript bind mount")
        ),
    )


def container_payload(
    *,
    worker_id: str,
    agent_type: str,
    exit_code: int | None,
    transcript_source: str,
    worker_type: str = "developer",
    run_id: str = RUN_ID,
    attempt_id: str = "task-1",
    created: datetime = RUN_START + timedelta(seconds=5),
    environment: tuple[str, ...] = (),
) -> dict:
    running = exit_code is None
    return {
        "Created": created.isoformat(),
        "Image": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
        "Config": {
            "Image": f"worker-base-{agent_type}:latest",
            "Labels": {
                WorkerLabel.ID.value: worker_id,
                WorkerLabel.TYPE.value: "worker",
                WorkerLabel.PROJECT.value: PROJECT_ID,
                WorkerLabel.RUN.value: run_id,
                WorkerLabel.ATTEMPT.value: attempt_id,
            },
            "Env": [
                f"WORKER_AGENT_TYPE={agent_type}",
                f"WORKER_TYPE={worker_type}",
                *environment,
            ],
        },
        "State": {
            "Status": "running" if running else "exited",
            "Running": running,
            "OOMKilled": False,
            "ExitCode": 0 if running else exit_code,
            "StartedAt": created.isoformat(),
            "FinishedAt": "0001-01-01T00:00:00Z"
            if running
            else (created + timedelta(seconds=42)).isoformat(),
            "Error": "",
        },
        "Mounts": [
            {"Destination": "/workspace", "Source": "/data/workspaces/x"},
            {"Destination": "/artifacts/worker-transcripts", "Source": transcript_source},
        ],
    }


@pytest.fixture
def transcripts(tmp_path: Path) -> Path:
    directory = tmp_path / "worker-transcripts" / DEV_WORKER_ID
    directory.mkdir(parents=True)
    (directory / "req-1.log").write_text("--- stdout ---\n--- stderr ---\n", encoding="utf-8")
    return tmp_path / "worker-transcripts"


@pytest.fixture
def codex_docker(transcripts: Path) -> FakeDocker:
    return FakeDocker(
        containers={
            DEV_CONTAINER: container_payload(
                worker_id=DEV_WORKER_ID,
                agent_type="codex",
                exit_code=0,
                transcript_source=str(transcripts),
            )
        },
        logs={DEV_CONTAINER: "agent_exited_without_result worker_id=" + DEV_WORKER_ID},
    )


def collector_for(
    docker: FakeDocker, *, now: datetime = RUN_START, **kwargs
) -> RunEvidenceCollector:
    ticks = [now]

    def clock() -> datetime:
        ticks[0] = ticks[0] + timedelta(seconds=1)
        return ticks[0]

    return RunEvidenceCollector(run_id=RUN_ID, probe=docker.probe(), clock=clock, **kwargs)


def base_ctx(collector: RunEvidenceCollector, **overrides) -> dict:
    ctx = {
        "project_id": PROJECT_ID,
        "project_name": REPO,
        "repo_name": REPO,
        "story_id": "story-1",
        "task_id": "task-1",
        "agent_type": "codex",
        "qa_agent_type_requested": "claude",
        "qa_requires_executor": True,
        "qa_agent_type": "claude",
        "scaffold_status": ProjectStatus.ACTIVE,
        "task_status": TaskStatus.FAILED,
        "engineering_elapsed": 85,
        "run_evidence": collector,
    }
    ctx.update(overrides)
    return ctx


def test_a_worker_never_sampled_alive_is_still_fully_attributed(codex_docker, tmp_path):
    collector = collector_for(codex_docker)

    collector.capture()

    assert codex_docker.listed_states == ["exited"]  # never seen running
    worker = build_artifact(base_ctx(collector), root=tmp_path)["workers"][0]
    assert worker["discovered_by"] == Discovery.RUN_LABEL.value
    assert worker["exit_code"] == {"status": "captured", "value": 0, "reason": None}
    assert "agent_exited_without_result" in worker["log_tail"]["value"]["text"]
    assert worker["agent_type_executed"]["value"] == "codex"
    assert worker["ownership_labels"] == {
        WorkerLabel.PROJECT.value: PROJECT_ID,
        WorkerLabel.RUN.value: RUN_ID,
        WorkerLabel.ATTEMPT.value: "task-1",
    }


def test_a_dead_worker_is_read_after_its_redis_metadata_is_gone(codex_docker, tmp_path):
    collector = collector_for(codex_docker, owned_workers=lambda: [])
    collector.capture()

    worker = build_artifact(base_ctx(collector), root=tmp_path)["workers"][0]
    assert worker["worker_id"] == DEV_WORKER_ID
    assert worker["exit_code"]["value"] == 0


def test_another_runs_worker_is_never_this_runs_evidence(transcripts, tmp_path):
    docker = FakeDocker()
    docker.add(
        DEV_CONTAINER,
        container_payload(
            worker_id=DEV_WORKER_ID,
            agent_type="codex",
            exit_code=0,
            transcript_source=str(transcripts),
        ),
        "ours",
    )
    docker.add(
        "worker-dev-other-11112222",
        container_payload(
            worker_id="dev-other-11112222",
            agent_type="claude",
            exit_code=0,
            transcript_source=str(transcripts),
            run_id="live-someone-else",
        ),
        "not ours",
    )
    docker.add(
        "worker-qa-old0old0old0",
        container_payload(
            worker_id="qa-old0old0old0",
            agent_type="claude",
            worker_type="qa",
            exit_code=0,
            transcript_source=str(transcripts),
            run_id="live-previous-combination",
            created=RUN_START - timedelta(minutes=5),
        ),
        "previous combination",
    )
    collector = collector_for(docker)
    collector.capture()

    workers = build_artifact(base_ctx(collector), root=tmp_path)["workers"]
    assert [worker["worker_id"] for worker in workers] == [DEV_WORKER_ID]


def test_a_qa_executor_of_this_run_needs_no_creation_window_to_be_claimed(transcripts, tmp_path):
    docker = FakeDocker()
    docker.add(
        QA_CONTAINER,
        container_payload(
            worker_id=QA_WORKER_ID,
            agent_type="claude",
            worker_type="qa",
            exit_code=3,
            transcript_source=str(transcripts),
            created=RUN_START - timedelta(minutes=5),
        ),
        "qa_executor_failed",
    )
    collector = collector_for(docker)
    collector.capture()

    worker = build_artifact(base_ctx(collector), root=tmp_path)["workers"][0]
    assert worker["worker_id"] == QA_WORKER_ID
    assert worker["role"] == WorkerRole.QA_EXECUTOR.value
    assert worker["role_evidence"] == RoleEvidence.CONTAINER_ENV.value
    assert worker["exit_code"]["value"] == 3


def test_role_falls_back_to_the_worker_id_when_no_container_can_be_read():
    assert role_from_worker_id("qa-abc123abc123") is WorkerRole.QA_EXECUTOR
    assert role_from_worker_id(DEV_WORKER_ID) is WorkerRole.DEVELOPER


def test_a_collector_without_a_run_id_is_refused():
    with pytest.raises(ValueError, match="scoped to a run id"):
        RunEvidenceCollector(run_id="")


def test_a_removed_container_the_run_owned_is_reported_not_omitted(codex_docker, tmp_path):
    manifest = OwnershipManifest(run_id=RUN_ID)
    manifest.own("worker", DEV_WORKER_ID)
    manifest.own("worker", "qa-ffffffffffff")
    ctx = base_ctx(collector_for(codex_docker), manifest=manifest)

    path = emit_run_evidence(ctx, root=tmp_path)

    artifact = json.loads(path.read_text(encoding="utf-8"))
    by_id = {worker["worker_id"]: worker for worker in artifact["workers"]}
    assert by_id[DEV_WORKER_ID]["exit_code"]["value"] == 0  # captured, not overwritten
    absent = by_id["qa-ffffffffffff"]
    assert absent["role"] == WorkerRole.QA_EXECUTOR.value
    assert absent["discovered_by"] == Discovery.OWNERSHIP_MANIFEST.value
    assert absent["container_present"] is False
    for level in ("exit_code", "log_tail", "state"):
        assert absent[level]["status"] == CaptureStatus.MISSED.value
        assert absent[level]["value"] is None
        assert "never listed its container" in absent[level]["reason"]
    assert path.parent == tmp_path / "docs" / "e2e_results"


def test_the_artifact_is_written_without_being_told_where(tmp_path, monkeypatch):
    monkeypatch.setattr(run_evidence, "orchestrator_root", lambda: tmp_path)
    ctx = base_ctx(collector_for(FakeDocker()))

    path = emit_run_evidence(ctx)

    assert path.parent == tmp_path / "docs" / "e2e_results"
    assert json.loads(path.read_text(encoding="utf-8"))["kind"] == EVIDENCE_KIND


def test_stand_runner_can_route_evidence_into_its_ephemeral_run_directory(tmp_path, monkeypatch):
    output = tmp_path / "stand-run"
    monkeypatch.setenv(run_evidence.LIVE_EVIDENCE_OUTPUT_DIR_ENV, str(output))
    ctx = base_ctx(collector_for(FakeDocker()))

    path = emit_run_evidence(ctx)

    assert evidence_output_directory() == output
    assert path.parent == output
    assert path.name.startswith("run-evidence-")


def test_artifact_retains_redacted_task_failure_diagnostics(tmp_path):
    collector = collector_for(FakeDocker())
    ctx = base_ctx(collector)
    ctx["task_diagnostics"] = {
        "task-1": {
            "status": "waiting_human_review",
            "current_iteration": 3,
            "max_iterations": 3,
            "failure_metadata": {"reason": "worker exited 7"},
        }
    }

    artifact = build_artifact(ctx, root=tmp_path)

    assert artifact["tasks"] == ctx["task_diagnostics"]


def test_an_owned_worker_the_label_never_listed_is_accounted_for_on_every_pass(tmp_path):
    docker = FakeDocker()
    collector = RunEvidenceCollector(
        run_id=RUN_ID,
        probe=docker.probe(),
        owned_workers=lambda: [DEV_WORKER_ID, "qa-ffffffffffff"],
        clock=lambda: RUN_START,
    )
    collector.capture()

    artifact = build_artifact(base_ctx(collector), root=tmp_path)
    by_id = {worker["worker_id"]: worker for worker in artifact["workers"]}
    assert set(by_id) == {DEV_WORKER_ID, "qa-ffffffffffff"}
    assert by_id[DEV_WORKER_ID]["role"] == WorkerRole.DEVELOPER.value
    assert by_id[DEV_WORKER_ID]["role_evidence"] == RoleEvidence.WORKER_ID.value
    assert artifact["run"]["attempts"] == 1


def test_a_sampled_worker_keeps_its_capture_over_the_manifest(codex_docker, tmp_path):
    collector = RunEvidenceCollector(
        run_id=RUN_ID,
        probe=codex_docker.probe(),
        owned_workers=lambda: [DEV_WORKER_ID],
        clock=lambda: RUN_START,
    )
    collector.capture()
    codex_docker.remove(DEV_CONTAINER)
    collector.capture()

    worker = build_artifact(base_ctx(collector), root=tmp_path)["workers"][0]
    assert worker["exit_code"]["value"] == 0
    assert "agent_exited_without_result" in worker["log_tail"]["value"]["text"]


def test_a_worker_listed_running_that_is_then_removed_states_the_loss(transcripts, tmp_path):
    docker = FakeDocker(
        containers={
            DEV_CONTAINER: container_payload(
                worker_id=DEV_WORKER_ID,
                agent_type="codex",
                exit_code=None,
                transcript_source=str(transcripts),
            )
        },
        logs={DEV_CONTAINER: "worker_started worker_id=" + DEV_WORKER_ID},
    )
    collector = collector_for(docker)
    collector.capture()
    docker.remove(DEV_CONTAINER)
    collector.capture()

    worker = build_artifact(base_ctx(collector), root=tmp_path)["workers"][0]
    assert worker["container_present"] is False
    assert worker["exit_code"]["status"] == CaptureStatus.MISSED.value
    assert "removed between two evidence passes" in worker["exit_code"]["reason"]
    assert "worker_started" in worker["log_tail"]["value"]["text"]
    assert worker["agent_type_executed"]["value"] == "codex"


def test_a_container_removed_between_listing_and_inspection_says_so(codex_docker, tmp_path):
    listed = ListedWorker(
        container=DEV_CONTAINER,
        worker_id=DEV_WORKER_ID,
        ownership={WorkerLabel.RUN.value: RUN_ID},
    )
    probe = codex_docker.probe()
    collector = collector_for(codex_docker)
    codex_docker.remove(DEV_CONTAINER)

    record = capture_worker(probe, listed, now=RUN_START)
    assert record["container_present"] is False
    assert record["discovered_by"] == Discovery.RUN_LABEL.value
    assert "removed between the listing and its inspection" in record["exit_code"]["reason"]
    assert collector.errors == []


def test_a_failed_listing_does_not_declare_every_container_lost(transcripts, tmp_path):
    docker = FakeDocker(
        containers={
            DEV_CONTAINER: container_payload(
                worker_id=DEV_WORKER_ID,
                agent_type="claude",
                exit_code=None,
                transcript_source=str(transcripts),
            )
        },
        logs={DEV_CONTAINER: "worker_started"},
    )
    collector = collector_for(docker)
    collector.capture()
    docker.listing_error = "docker daemon unreachable"
    collector.capture()

    artifact = build_artifact(base_ctx(collector), root=tmp_path)
    worker = artifact["workers"][0]
    assert worker["container_present"] is True
    assert "still running" in worker["exit_code"]["reason"]
    assert artifact["capture_errors"] == [
        "worker container discovery failed: docker daemon unreachable"
    ]


def test_probe_failure_is_recorded_not_raised(tmp_path):

    def explode(run_id: str) -> list[ListedWorker]:
        raise RuntimeError("docker daemon unreachable")

    collector = RunEvidenceCollector(
        run_id=RUN_ID,
        probe=ContainerProbe(
            list_run_workers=explode,
            inspect=lambda name: None,
            logs=lambda name, tail: None,
            removed_workers=lambda run_id: [],
        ),
        clock=lambda: RUN_START,
    )
    collector.capture()

    artifact = build_artifact(base_ctx(collector), root=tmp_path)
    assert artifact["capture_errors"] == [
        "worker container discovery failed: docker daemon unreachable"
    ]
    assert artifact["workers"] == []
    assert artifact["combination"]["worker_executed"]["status"] == CaptureStatus.MISSED.value


def test_ownership_reconciliation_failure_is_recorded_not_raised(codex_docker, tmp_path):
    def explode() -> list[str]:
        raise RuntimeError("manifest unreadable")

    collector = collector_for(codex_docker, owned_workers=explode)
    collector.capture()

    artifact = build_artifact(base_ctx(collector), root=tmp_path)
    assert artifact["capture_errors"] == ["owned worker reconciliation failed: manifest unreadable"]
    assert artifact["workers"][0]["exit_code"]["value"] == 0


def test_a_worker_deleted_before_the_first_pass_still_carries_its_exit(codex_docker, tmp_path):
    codex_docker.delete(DEV_CONTAINER, exit_code=137)
    assert codex_docker.containers == {}

    collector = collector_for(codex_docker)
    collector.capture()

    artifact = build_artifact(base_ctx(collector), root=tmp_path)
    assert [worker["worker_id"] for worker in artifact["workers"]] == [DEV_WORKER_ID]
    worker = artifact["workers"][0]
    assert worker["discovered_by"] == Discovery.DELETE_CAPTURE.value
    assert worker["role_evidence"] == RoleEvidence.DELETE_RECORD.value
    assert worker["container_present"] is False
    assert worker["exit_code"] == {
        "status": CaptureStatus.CAPTURED.value,
        "value": 137,
        "reason": None,
    }
    assert worker["log_tail"]["value"]["text"] == "wrapper said something\n"
    assert worker["agent_type_executed"]["value"] == "codex"
    assert worker["delete_reason"] == "failed"
    assert worker["ownership_labels"][WorkerLabel.RUN.value] == RUN_ID
    assert worker["transcript"]["host_dir"]["value"].endswith(DEV_WORKER_ID)
    assert artifact["capture_errors"] == []
    assert artifact["run"]["attempts"] == 1
    assert artifact["combination"]["worker_executed"]["value"] == "codex"


def test_a_removed_qa_executor_makes_the_qa_cell_answer_for_a_real_container(
    codex_docker, tmp_path
):
    codex_docker.delete(
        QA_CONTAINER, worker_type="qa", agent_type="claude", exit_code=0, transcript_dir=None
    )

    collector = collector_for(codex_docker)
    collector.capture()

    artifact = build_artifact(base_ctx(collector), root=tmp_path)
    qa = artifact["qa"]
    assert qa["executor_workers"] == [QA_WORKER_ID]
    assert qa["executor_executed"]["value"] == "claude"
    record = next(w for w in artifact["workers"] if w["worker_id"] == QA_WORKER_ID)
    assert record["role"] == WorkerRole.QA_EXECUTOR.value
    assert record["transcript"]["host_dir"]["status"] == CaptureStatus.MISSED.value
    assert record["transcript"]["host_dir"]["reason"]


def test_a_removal_record_that_missed_the_exit_says_so_rather_than_nothing(codex_docker, tmp_path):
    codex_docker.delete(
        DEV_CONTAINER,
        exit_code=None,
        exit_reason="the container was still running when it was removed",
        log_tail=None,
    )

    collector = collector_for(codex_docker)
    collector.capture()

    artifact = build_artifact(base_ctx(collector), root=tmp_path)
    worker = artifact["workers"][0]
    assert worker["exit_code"]["status"] == CaptureStatus.MISSED.value
    assert worker["exit_code"]["value"] is None
    assert "still running when it was removed" in worker["exit_code"]["reason"]
    assert worker["log_tail"]["status"] == CaptureStatus.MISSED.value
    assert worker["log_tail"]["reason"]


def test_a_removal_record_never_overwrites_what_a_pass_read_off_the_container(
    codex_docker, tmp_path
):
    collector = collector_for(codex_docker)
    collector.capture()
    codex_docker.delete(DEV_CONTAINER, exit_code=137)

    collector.capture()

    artifact = build_artifact(base_ctx(collector), root=tmp_path)
    worker = artifact["workers"][0]
    assert worker["discovered_by"] == Discovery.RUN_LABEL.value
    assert worker["exit_code"]["value"] == 0
    assert worker["transcript"]["files"]["value"]


def test_a_running_container_that_is_deleted_is_completed_by_its_record(transcripts, tmp_path):
    docker = FakeDocker(
        containers={
            DEV_CONTAINER: container_payload(
                worker_id=DEV_WORKER_ID,
                agent_type="codex",
                exit_code=None,
                transcript_source=str(transcripts),
            )
        },
        logs={DEV_CONTAINER: "still working"},
    )
    collector = collector_for(docker)
    collector.capture()
    docker.delete(DEV_CONTAINER, exit_code=143)

    collector.capture()

    artifact = build_artifact(base_ctx(collector), root=tmp_path)
    worker = artifact["workers"][0]
    assert worker["exit_code"]["value"] == 143
    assert worker["discovered_by"] == Discovery.DELETE_CAPTURE.value


def test_one_runs_removal_records_are_never_read_into_another_run(codex_docker, tmp_path):
    codex_docker.delete(DEV_CONTAINER, exit_code=137, run_id="live-someone-else")

    collector = collector_for(codex_docker)
    collector.capture()

    artifact = build_artifact(base_ctx(collector), root=tmp_path)
    assert artifact["workers"] == []


def test_a_removal_record_read_failure_is_recorded_not_raised(codex_docker, tmp_path):
    codex_docker.removal_record_error = "redis is unreachable"

    collector = collector_for(codex_docker)
    collector.capture()

    artifact = build_artifact(base_ctx(collector), root=tmp_path)
    assert artifact["capture_errors"] == [
        "removed worker evidence read failed: redis is unreachable"
    ]
    assert artifact["workers"][0]["exit_code"]["value"] == 0


def test_a_worker_in_no_source_at_all_is_still_named_with_its_reason(codex_docker, tmp_path):
    codex_docker.delete(DEV_CONTAINER, exit_code=137)
    codex_docker.removed.clear()  # the capture itself never reached Redis

    collector = collector_for(codex_docker, owned_workers=lambda: [DEV_WORKER_ID])
    collector.capture()

    artifact = build_artifact(base_ctx(collector), root=tmp_path)
    worker = artifact["workers"][0]
    assert worker["discovered_by"] == Discovery.OWNERSHIP_MANIFEST.value
    assert worker["exit_code"]["status"] == CaptureStatus.MISSED.value
    assert "no removal record was written for it" in worker["exit_code"]["reason"]


def test_running_container_is_not_reported_as_exit_zero(transcripts, tmp_path):
    docker = FakeDocker(
        containers={
            DEV_CONTAINER: container_payload(
                worker_id=DEV_WORKER_ID,
                agent_type="claude",
                exit_code=None,
                transcript_source=str(transcripts),
            )
        },
        logs={DEV_CONTAINER: "worker_started"},
    )
    collector = collector_for(docker)
    collector.capture()

    worker = build_artifact(base_ctx(collector), root=tmp_path)["workers"][0]
    assert worker["exit_code"]["status"] == CaptureStatus.MISSED.value
    assert "still running" in worker["exit_code"]["reason"]
    assert worker["state"]["value"]["running"] is True


def test_a_container_that_never_started_is_not_reported_as_exit_zero(transcripts, tmp_path):
    payload = container_payload(
        worker_id=DEV_WORKER_ID,
        agent_type="codex",
        exit_code=0,
        transcript_source=str(transcripts),
    )
    payload["State"]["Status"] = "created"
    docker = FakeDocker(containers={DEV_CONTAINER: payload}, logs={DEV_CONTAINER: ""})
    collector = collector_for(docker)
    collector.capture()

    worker = build_artifact(base_ctx(collector), root=tmp_path)["workers"][0]
    assert worker["exit_code"]["status"] == CaptureStatus.MISSED.value
    assert "never started" in worker["exit_code"]["reason"]


def test_a_later_pass_cannot_erase_a_captured_exit(codex_docker, tmp_path):
    collector = collector_for(codex_docker)
    collector.capture()
    codex_docker.remove(DEV_CONTAINER)
    collector.capture()
    collector.capture()

    workers = build_artifact(base_ctx(collector), root=tmp_path)["workers"]
    assert len(workers) == 1
    assert workers[0]["exit_code"]["value"] == 0


def test_attempts_counts_every_developer_container(transcripts, tmp_path):
    second_id = f"dev-{REPO[:20]}-aabbccdd"
    docker = FakeDocker(
        containers={
            DEV_CONTAINER: container_payload(
                worker_id=DEV_WORKER_ID,
                agent_type="codex",
                exit_code=0,
                transcript_source=str(transcripts),
            )
        },
        logs={DEV_CONTAINER: "first attempt"},
    )
    docker.add(
        f"worker-{second_id}",
        container_payload(
            worker_id=second_id,
            agent_type="codex",
            exit_code=1,
            transcript_source=str(transcripts),
            attempt_id="task-2",
            created=RUN_START + timedelta(seconds=120),
        ),
        "second attempt",
    )
    collector = collector_for(docker)
    collector.capture()

    artifact = build_artifact(base_ctx(collector), root=tmp_path)
    assert artifact["run"]["attempts"] == 2
    assert sorted(worker["exit_code"]["value"] for worker in artifact["workers"]) == [0, 1]
    assert {
        worker["ownership_labels"][WorkerLabel.ATTEMPT.value] for worker in artifact["workers"]
    } == {"task-1", "task-2"}


def test_qa_cell_is_not_exercised_when_the_worker_died_first(codex_docker, tmp_path):
    collector = collector_for(codex_docker)
    collector.capture()

    cell = build_artifact(base_ctx(collector), root=tmp_path)["qa"]
    assert cell["state"] == QAExercise.NOT_EXERCISED.value
    assert cell["reason"] == (
        "the worker died before QA: the engineering task ended failed, "
        "so nothing was ever handed to QA"
    )
    assert cell["run_id"] is None
    assert cell["outcome"] is None
    assert cell["executor_selected"] == "claude"
    assert cell["executor_executed"]["status"] == CaptureStatus.MISSED.value
    assert cell["executor_executed"]["value"] is None
    assert (
        "no QA executor container of this run was observed" in (cell["executor_executed"]["reason"])
    )
    assert "'claude'" in cell["executor_executed"]["reason"]
    assert cell["executor_workers"] == []


def test_qa_executor_evidence_survives_the_delete_its_own_client_enqueues(
    codex_docker, transcripts, tmp_path
):
    codex_docker.add(
        QA_CONTAINER,
        container_payload(
            worker_id=QA_WORKER_ID,
            agent_type="claude",
            worker_type="qa",
            exit_code=3,
            transcript_source=str(transcripts),
            attempt_id="qa-run-1",
            created=RUN_START + timedelta(minutes=2),
        ),
        "qa_executor_failed",
    )
    collector = collector_for(codex_docker)
    collector.capture()

    ctx = base_ctx(
        collector,
        task_status=TaskStatus.DONE,
        deploy_outcome=DeployOutcome.SUCCESS.value,
        final_app_status=ApplicationStatus.RUNNING.value,
        qa_run={"id": "qa-run-1", "result": {"qa_outcome": QAOutcome.FAILED.value}},
    )
    artifact = build_artifact(ctx, root=tmp_path)
    cell = artifact["qa"]
    assert cell["state"] == QAExercise.EXERCISED.value
    assert cell["executor_executed"]["value"] == "claude"
    assert cell["executor_workers"] == [QA_WORKER_ID]
    qa_worker = next(
        worker for worker in artifact["workers"] if worker["role"] == WorkerRole.QA_EXECUTOR.value
    )
    assert qa_worker["exit_code"]["value"] == 3
    assert "qa_executor_failed" in qa_worker["log_tail"]["value"]["text"]


def test_qa_executor_that_was_never_observed_is_not_claimed_from_configuration(
    codex_docker, tmp_path
):
    collector = collector_for(codex_docker)
    collector.capture()
    ctx = base_ctx(
        collector,
        task_status=TaskStatus.DONE,
        deploy_outcome=DeployOutcome.SUCCESS.value,
        final_app_status=ApplicationStatus.RUNNING.value,
        qa_run={"id": "qa-run-1", "result": {"qa_outcome": QAOutcome.PASSED.value}},
    )

    artifact = build_artifact(ctx, root=tmp_path)
    cell = artifact["qa"]
    assert cell["state"] == QAExercise.EXERCISED.value
    assert cell["executor_selected"] == "claude"
    assert cell["executor_executed"]["status"] == CaptureStatus.MISSED.value
    assert "not evidenced" in cell["executor_executed"]["reason"]
    assert artifact["combination"]["qa_executed"] == cell["executor_executed"]


def test_qa_cell_is_not_exercised_when_the_deploy_never_ran(codex_docker):
    cell = qa_cell(
        base_ctx(
            collector_for(codex_docker),
            task_status=TaskStatus.DONE,
            deploy_outcome=DeployOutcome.SMOKE_FAILURE.value,
            final_app_status=ApplicationStatus.DOWN.value,
        )
    )
    assert cell["state"] == QAExercise.NOT_EXERCISED.value
    assert "never reached a running deployment" in cell["reason"]


def test_qa_cell_is_exercised_only_with_a_terminal_qa_run(codex_docker):
    ctx = base_ctx(
        collector_for(codex_docker),
        task_status=TaskStatus.DONE,
        deploy_outcome=DeployOutcome.SUCCESS.value,
        final_app_status=ApplicationStatus.RUNNING.value,
    )
    assert qa_cell(ctx)["state"] == QAExercise.NOT_EXERCISED.value

    ctx["qa_run"] = {"id": "qa-run-1", "result": {"qa_outcome": QAOutcome.FAILED.value}}
    cell = qa_cell(ctx)
    assert cell["state"] == QAExercise.EXERCISED.value
    assert cell["run_id"] == "qa-run-1"
    assert cell["outcome"] == QAOutcome.FAILED.value


def test_deterministic_qa_declares_that_it_starts_no_executor(codex_docker):
    cell = qa_cell(
        base_ctx(
            collector_for(codex_docker),
            qa_requires_executor=False,
            qa_agent_type=None,
            qa_agent_type_requested=None,
        )
    )
    assert cell["mode"] == "deterministic_health"
    assert cell["executor_executed"]["status"] == CaptureStatus.MISSED.value
    assert "starts no QA executor" in cell["executor_executed"]["reason"]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {"scaffold_status": ProjectStatus.DRAFT},
            (TerminalState.STOPPED_AT_SCAFFOLD, FailureKind.SCAFFOLD_FAILED),
        ),
        (
            {"env_contract_errors": {"scaffold": "no fragments"}},
            (TerminalState.STOPPED_AT_SCAFFOLD, FailureKind.ENV_CONTRACT_MISSING),
        ),
        (
            {"task_status": TaskStatus.FAILED},
            (TerminalState.STOPPED_AT_ENGINEERING, FailureKind.WORKER_DID_NOT_FINISH),
        ),
        (
            {"task_status": TaskStatus.DONE},
            (TerminalState.STOPPED_AT_DEPLOY, FailureKind.DEPLOY_RUN_MISSING),
        ),
        (
            {
                "task_status": TaskStatus.DONE,
                "deploy_run_id": "run-1",
                "env_contract_errors": {"merged": "fragment lost"},
            },
            (TerminalState.STOPPED_AT_DEPLOY, FailureKind.ENV_CONTRACT_MISSING),
        ),
        (
            {
                "task_status": TaskStatus.DONE,
                "deploy_run_id": "run-1",
                "deploy_outcome": DeployOutcome.SMOKE_FAILURE.value,
            },
            (TerminalState.STOPPED_AT_DEPLOY, FailureKind.DEPLOY_FAILED),
        ),
        (
            {
                "task_status": TaskStatus.DONE,
                "deploy_run_id": "run-1",
                "deploy_outcome": DeployOutcome.SUCCESS.value,
                "final_app_status": ApplicationStatus.RUNNING.value,
            },
            (TerminalState.STOPPED_AT_QA, FailureKind.QA_NEVER_RAN),
        ),
        (
            {
                "task_status": TaskStatus.DONE,
                "deploy_run_id": "run-1",
                "deploy_outcome": DeployOutcome.SUCCESS.value,
                "final_app_status": ApplicationStatus.RUNNING.value,
                "qa_run": {"id": "q", "result": {"qa_outcome": QAOutcome.FAILED.value}},
            },
            (TerminalState.STOPPED_AT_QA, FailureKind.QA_NOT_PASSED),
        ),
        (
            {
                "task_status": TaskStatus.DONE,
                "deploy_run_id": "run-1",
                "deploy_outcome": DeployOutcome.SUCCESS.value,
                "final_app_status": ApplicationStatus.RUNNING.value,
                "qa_run": {"id": "q", "result": {"qa_outcome": QAOutcome.PASSED.value}},
            },
            (TerminalState.COMPLETED, FailureKind.NONE),
        ),
    ],
)
def test_classify_outcome(codex_docker, overrides, expected):
    assert classify_outcome(base_ctx(collector_for(codex_docker), **overrides)) == expected


def test_log_tail_redacts_secret_environment_values():
    environment = {
        "GITHUB_TOKEN": "ghp_supersecret",
        "WORKER_BROKER_TOKEN": "broker-secret",
        "WORKER_AGENT_TYPE": "codex",
    }
    tail = redact_log_tail(
        "pushed with ghp_supersecret using broker-secret as codex\n"
        "Authorization: Bearer abcdef\n"
        "clone https://user:pw@github.com/org/repo",
        environment,
    )
    assert "ghp_supersecret" not in tail
    assert "broker-secret" not in tail
    assert "abcdef" not in tail
    assert "user:pw@" not in tail
    assert "as codex" in tail


def test_log_tail_is_bounded():
    assert len(redact_log_tail("x" * (LOG_TAIL_MAX_CHARS * 2), {})) == LOG_TAIL_MAX_CHARS


def test_artifact_carries_no_agent_stdout_only_a_transcript_pointer(codex_docker, tmp_path):
    collector = collector_for(codex_docker)
    collector.capture()

    worker = build_artifact(base_ctx(collector), root=tmp_path)["workers"][0]
    transcript = worker["transcript"]
    assert transcript["host_dir"]["value"].endswith(f"/{DEV_WORKER_ID}")
    assert [Path(item["path"]).name for item in transcript["files"]["value"]] == ["req-1.log"]
    assert "agent_stdout_tail" not in json.dumps(worker)


def test_unreadable_transcript_directory_says_why(transcripts, tmp_path):
    docker = FakeDocker(
        containers={
            DEV_CONTAINER: container_payload(
                worker_id=DEV_WORKER_ID,
                agent_type="codex",
                exit_code=0,
                transcript_source="/no/such/transcript/root",
            )
        },
        logs={DEV_CONTAINER: "line"},
    )
    collector = collector_for(docker)
    collector.capture()

    transcript = build_artifact(base_ctx(collector), root=tmp_path)["workers"][0]["transcript"]
    assert transcript["host_dir"]["value"] == f"/no/such/transcript/root/{DEV_WORKER_ID}"
    assert transcript["files"]["value"] == []


def test_capture_cannot_be_empty_without_a_reason():
    with pytest.raises(ValueError, match="must say why"):
        Capture(CaptureStatus.MISSED)
    with pytest.raises(ValueError, match="carries no value"):
        Capture(CaptureStatus.MISSED, value=3, reason="gone")
    with pytest.raises(ValueError, match="carries no reason"):
        Capture(CaptureStatus.CAPTURED, value=3, reason="gone")


def test_artifact_schema_field_by_field(codex_docker, tmp_path):
    (tmp_path / "deployed-worker-images.json").write_text(
        json.dumps(
            {
                "git_sha": "4b220830",
                "source_hash": "abc123",
                "images": {
                    "worker-base-codex": {
                        "reference": f"{CODEX_REPOSITORY}@sha256:dead",
                        "repository": CODEX_REPOSITORY,
                        "digest": "sha256:dead",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    collector = collector_for(codex_docker)
    collector.capture()
    ctx = base_ctx(collector)

    artifact = build_artifact(ctx, root=tmp_path, now=RUN_START + timedelta(seconds=300))

    assert artifact["schema_version"] == EVIDENCE_SCHEMA_VERSION
    assert artifact["kind"] == EVIDENCE_KIND
    assert artifact["generated_at"] == "2026-08-13T12:05:00+00:00"

    assert artifact["combination"]["label"] == "worker-codex-qa-claude"
    assert artifact["combination"]["worker_requested"] == "codex"
    assert artifact["combination"]["worker_executed"]["value"] == "codex"
    assert artifact["combination"]["qa_requested"] == "claude"
    assert artifact["combination"]["qa_executed"] == artifact["qa"]["executor_executed"]
    assert artifact["combination"]["qa_executed"]["status"] == CaptureStatus.MISSED.value

    assert artifact["project"] == {
        "id": PROJECT_ID,
        "name": REPO,
        "story_id": "story-1",
        "task_id": "task-1",
    }

    assert artifact["discovery"]["run_id"] == RUN_ID
    assert artifact["discovery"]["docker_filters"] == [
        "label=com.codegen.type=worker",
        f"label=com.codegen.run.id={RUN_ID}",
    ]

    release = artifact["release"]
    assert release["deployed_sha"]["value"] == "4b220830"
    assert release["record"]["value"]["images"]["worker-base-codex"]["digest"] == "sha256:dead"
    assert release["checkout_sha"]["status"] in {status.value for status in CaptureStatus}

    run = artifact["run"]
    assert run["id"] == RUN_ID
    assert run["started_at"] == collector.started_at.isoformat()
    assert run["finished_at"] == "2026-08-13T12:05:00+00:00"
    assert run["duration_seconds"] == 299.0
    assert run["engineering_elapsed_seconds"] == 85
    assert run["attempts"] == 1
    assert run["terminal_state"] == TerminalState.STOPPED_AT_ENGINEERING.value
    assert run["failure_kind"] == FailureKind.WORKER_DID_NOT_FINISH.value
    assert run["task_status"] == TaskStatus.FAILED

    assert artifact["failure"] == {
        "failed": True,
        "stage": TerminalState.STOPPED_AT_ENGINEERING.value,
        "failure_kind": FailureKind.WORKER_DID_NOT_FINISH.value,
        "control_plane_reason": artifact["failure"]["control_plane_reason"],
    }
    assert artifact["verdict"]["paid"] is True
    assert artifact["verdict"]["status"] == "red"
    assert artifact["engineering"] == {
        "collection": {
            "status": CaptureStatus.MISSED.value,
            "value": None,
            "reason": run_evidence.ENGINEERING_EVIDENCE_ESCAPED_REASON.format(
                subject=f"engineering task {ctx['task_id']}, last observed status "
                f"{TaskStatus.FAILED}"
            ),
        },
        "runs": [],
    }
    assert artifact["debug_dumps"] == []

    assert artifact["qa"]["state"] == QAExercise.NOT_EXERCISED.value
    assert artifact["capture_errors"] == []
    assert "redact_diagnostic" in artifact["privacy"]

    worker = artifact["workers"][0]
    assert set(worker) == {
        "worker_id",
        "role",
        "role_evidence",
        "discovered_by",
        "ownership_labels",
        "container",
        "container_present",
        "agent_type_executed",
        "image",
        "created_at",
        "state",
        "exit_code",
        "log_tail",
        "transcript",
        "captured_at",
    }
    assert worker["worker_id"] == DEV_WORKER_ID
    assert worker["role"] == WorkerRole.DEVELOPER.value
    assert worker["container"] == DEV_CONTAINER
    assert worker["image"]["value"]["tag"] == "worker-base-codex:latest"
    assert worker["state"]["value"]["status"] == "exited"


def test_missing_release_record_is_reported_not_guessed(codex_docker, tmp_path):
    release = build_artifact(base_ctx(collector_for(codex_docker)), root=tmp_path)["release"]
    assert release["record"]["status"] == CaptureStatus.MISSED.value
    assert "was not deployed by deploy.yml" in release["record"]["reason"]
    assert release["deployed_sha"]["value"] is None


def test_written_artifact_is_json_named_after_its_combination(codex_docker, tmp_path):
    artifact = build_artifact(base_ctx(collector_for(codex_docker)), root=tmp_path, now=RUN_START)
    path = write_artifact(artifact, tmp_path / "e2e_results")

    assert path.name.startswith("run-evidence-worker-codex-qa-claude-")
    assert path.suffix == ".json"
    assert json.loads(path.read_text(encoding="utf-8")) == artifact


def test_combination_label_names_the_deterministic_qa_half():
    assert combination_label({"agent_type": "claude", "qa_requires_executor": False}) == (
        "worker-claude-qa-health"
    )


# ── The failing stage, its control-plane reason, and the verdict ─────────


ENGINEERING_RUN_ID = "run-eng-1"


def engineering_run_payload(**overrides) -> dict:
    """One engineering Run record as `/api/runs/` returns it."""
    run = {
        "id": ENGINEERING_RUN_ID,
        "type": "engineering",
        "task_id": "task-1",
        "status": "failed",
        "error_message": "Agent exited without reporting result",
        "run_metadata": {
            "stop_reason": "agent_limit_exceeded",
            "agent_limit_seconds": 1800,
            "executor_decision": {"agent_type": "codex", "source": "project_pin"},
        },
        "result": {"engineering_status": "failed"},
        "created_at": "2026-08-13T12:00:00+00:00",
        "updated_at": "2026-08-13T12:04:40+00:00",
    }
    run.update(overrides)
    return run


def engineering_records(
    *,
    run: Capture | None = None,
    admission: Capture | None = None,
    diagnostics: Capture | None = None,
) -> list[dict]:
    return [
        run_evidence.engineering_run_record(
            run_id=ENGINEERING_RUN_ID,
            task_id="task-1",
            run=run
            if run is not None
            else Capture.captured(run_evidence.engineering_run_facts(engineering_run_payload())),
            admission=admission
            if admission is not None
            else Capture.captured({"outcome": "admitted", "reason": None}),
            executor_diagnostics=diagnostics
            if diagnostics is not None
            else Capture.captured({"version": "v1", "diagnostics": []}),
        )
    ]


def test_a_story_branch_with_no_commit_is_the_reason_not_a_missing_deploy_run(
    codex_docker, tmp_path
):
    """The empty branch names itself, instead of an expired deploy wait naming it.

    Run 33706516335 ended `stopped_at_deploy` / `deploy_run_missing` after 420 s
    of waiting: true about the symptom, silent about the cause. A task the
    control plane called done over a branch carrying no commit stops at
    engineering, and the reason says so.
    """
    collector = collector_for(codex_docker)
    collector.capture()
    ctx = base_ctx(
        collector,
        task_status=TaskStatus.DONE,
        engineering_runs=engineering_records(),
        story_branch="story/story-1",
        story_branch_compare={
            "branch": "story/story-1",
            "status": "identical",
            "ahead_by": 0,
            "behind_by": 0,
        },
        story_branch_error=(
            "no commit was made for this story: story/story-1 is not ahead of main"
        ),
    )

    artifact = build_artifact(ctx, root=tmp_path)

    failure = artifact["failure"]
    assert failure["stage"] == TerminalState.STOPPED_AT_ENGINEERING.value
    assert failure["failure_kind"] == FailureKind.STORY_BRANCH_NOT_AHEAD.value
    reason = failure["control_plane_reason"]
    assert reason["value"]["source"] == run_evidence.ReasonSource.STORY_BRANCH.value
    assert reason["value"]["branch"] == "story/story-1"
    assert reason["value"]["compare"]["ahead_by"] == 0
    assert "no commit was made for this story" in reason["value"]["detail"]
    # The engineering Runs that called this task done travel with it.
    engineering = reason["value"]["engineering"]["value"]
    assert engineering["runs"][0]["run_id"] == ENGINEERING_RUN_ID
    assert artifact["run"]["failure_kind"] == FailureKind.STORY_BRANCH_NOT_AHEAD.value
    assert "no commit was made for this story" in artifact["qa"]["reason"]
    assert artifact["verdict"]["status"] == run_evidence.Verdict.RED.value


def test_a_story_branch_ahead_of_main_leaves_the_deploy_classification_alone(
    codex_docker, tmp_path
):
    """A branch that carries a commit is not a finding; the run classifies as before."""
    collector = collector_for(codex_docker)
    collector.capture()
    ctx = base_ctx(
        collector,
        task_status=TaskStatus.DONE,
        engineering_runs=engineering_records(),
        story_branch="story/story-1",
        story_branch_compare={
            "branch": "story/story-1",
            "status": "ahead",
            "ahead_by": 1,
            "behind_by": 0,
        },
    )

    artifact = build_artifact(ctx, root=tmp_path)

    assert artifact["failure"]["stage"] == TerminalState.STOPPED_AT_DEPLOY.value
    assert artifact["failure"]["failure_kind"] == FailureKind.DEPLOY_RUN_MISSING.value


def test_a_failed_paid_run_names_the_stage_and_the_control_plane_reason(codex_docker, tmp_path):
    collector = collector_for(codex_docker)
    collector.capture()
    ctx = base_ctx(
        collector,
        engineering_runs=engineering_records(),
        task_diagnostics={
            "task-1": {
                "status": TaskStatus.FAILED,
                "current_iteration": 0,
                "failure_metadata": None,
            }
        },
    )

    artifact = build_artifact(ctx, root=tmp_path)

    failure = artifact["failure"]
    assert failure["failed"] is True
    assert failure["stage"] == TerminalState.STOPPED_AT_ENGINEERING.value
    assert failure["failure_kind"] == FailureKind.WORKER_DID_NOT_FINISH.value
    reason = failure["control_plane_reason"]
    assert reason["status"] == CaptureStatus.CAPTURED.value
    assert reason["value"]["source"] == run_evidence.ReasonSource.ENGINEERING_RUN.value
    assert reason["value"]["task_status"] == TaskStatus.FAILED
    assert reason["value"]["task_failure_metadata"] is None
    assert reason["value"]["task_iteration"] == 0
    entry = reason["value"]["runs"][0]
    assert entry["run_id"] == ENGINEERING_RUN_ID
    assert entry["run_status"] == "failed"
    assert entry["error_message"] == "Agent exited without reporting result"
    assert entry["stop_reason"] == "agent_limit_exceeded"
    assert entry["executor_decision"] == {"agent_type": "codex", "source": "project_pin"}
    assert entry["admission_outcome"] == "admitted"


def test_the_engineering_section_carries_every_run_with_what_was_in_force(codex_docker, tmp_path):
    ctx = base_ctx(collector_for(codex_docker), engineering_runs=engineering_records())

    engineering = build_artifact(ctx, root=tmp_path)["engineering"]

    assert engineering["collection"] == {
        "status": CaptureStatus.CAPTURED.value,
        "value": {"runs_read": 1},
        "reason": None,
    }
    record = engineering["runs"][0]
    assert record["run_id"] == ENGINEERING_RUN_ID
    assert record["run"]["value"]["status"] == "failed"
    assert record["run"]["value"]["result"] == {"engineering_status": "failed"}
    assert record["admission"]["value"]["outcome"] == "admitted"
    assert record["executor_diagnostics"]["value"]["version"] == "v1"


def test_an_engineering_collection_failure_is_named_rather_than_omitted(codex_docker, tmp_path):
    ctx = base_ctx(
        collector_for(codex_docker),
        engineering_evidence_error="the engineering Runs could not be listed: ReadTimeout",
    )

    artifact = build_artifact(ctx, root=tmp_path)

    collection = artifact["engineering"]["collection"]
    assert collection["status"] == CaptureStatus.MISSED.value
    assert "ReadTimeout" in collection["reason"]
    reason = artifact["failure"]["control_plane_reason"]
    assert reason["status"] == CaptureStatus.MISSED.value
    assert "ReadTimeout" in reason["reason"]


def test_a_scaffold_stage_reason_carries_what_the_contract_probe_said(codex_docker, tmp_path):
    """The phase key alone is not a reason; for this stage the message is the reason."""
    ctx = base_ctx(
        collector_for(codex_docker),
        scaffold_status=ProjectStatus.ACTIVE,
        env_contract_errors={"scaffold": "services/backend/env.contract.yaml declares no entries"},
    )

    reason = build_artifact(ctx, root=tmp_path)["failure"]["control_plane_reason"]

    assert reason["status"] == CaptureStatus.CAPTURED.value
    assert reason["value"]["source"] == run_evidence.ReasonSource.SCAFFOLD.value
    assert reason["value"]["env_contract_errors"] == {
        "scaffold": "services/backend/env.contract.yaml declares no entries"
    }


def test_a_deploy_stage_reason_carries_what_the_contract_probe_said(codex_docker, tmp_path):
    ctx = base_ctx(
        collector_for(codex_docker),
        task_status=TaskStatus.DONE,
        deploy_run_id="run-1",
        env_contract_errors={"merged": "merged: fragments missing at abc123"},
    )

    reason = build_artifact(ctx, root=tmp_path)["failure"]["control_plane_reason"]

    assert reason["value"]["source"] == run_evidence.ReasonSource.DEPLOY_RUN.value
    assert reason["value"]["env_contract_errors"] == {
        "merged": "merged: fragments missing at abc123"
    }


def test_a_contract_probe_message_is_redacted_before_it_becomes_a_reason(
    codex_docker, tmp_path, monkeypatch
):
    """A probe's own text is a diagnostic like any other, held to the same rule."""
    monkeypatch.setenv("STAND_INTERNAL_API_KEY", "super-secret-token")
    ctx = base_ctx(
        collector_for(codex_docker),
        env_contract_errors={"scaffold": "probe could not run: auth super-secret-token rejected"},
    )

    reason = build_artifact(ctx, root=tmp_path)["failure"]["control_plane_reason"]

    assert "super-secret-token" not in json.dumps(reason)
    assert "[redacted]" in reason["value"]["env_contract_errors"]["scaffold"]


def test_a_stage_with_no_engineering_run_says_the_control_plane_holds_none(codex_docker, tmp_path):
    ctx = base_ctx(collector_for(codex_docker), engineering_runs=[])

    reason = build_artifact(ctx, root=tmp_path)["failure"]["control_plane_reason"]

    assert reason["status"] == CaptureStatus.MISSED.value
    assert "holds no engineering Run" in reason["reason"]


def test_an_error_escaping_the_engineering_phase_does_not_claim_it_was_never_reached(
    codex_docker, tmp_path
):
    """The reviewer's transient poll failure: entered, collected nothing, said so.

    `record_engineering_evidence` never runs, so the artifact carries a stated
    `missed`. What it must not do is assert the run never reached engineering:
    it did, and Run records the artifact never read may well exist.
    """
    ctx = base_ctx(collector_for(codex_docker))

    artifact = build_artifact(ctx, root=tmp_path)
    engineering = artifact["engineering"]

    assert engineering["collection"]["status"] == CaptureStatus.MISSED.value
    reason = engineering["collection"]["reason"]
    assert "entered the engineering phase" in reason
    assert "never reached" not in reason
    assert ctx["task_id"] in reason
    assert TaskStatus.FAILED in reason
    assert engineering["runs"] == []
    assert (
        "entered the engineering phase" in (artifact["failure"]["control_plane_reason"]["reason"])
    )


def test_a_run_that_never_entered_the_engineering_phase_says_exactly_that(codex_docker, tmp_path):
    ctx = base_ctx(collector_for(codex_docker))
    del ctx["task_id"]
    del ctx["story_id"]

    engineering = build_artifact(ctx, root=tmp_path)["engineering"]

    assert engineering["collection"]["reason"] == (
        run_evidence.ENGINEERING_PHASE_NEVER_ENTERED_REASON
    )
    assert engineering["runs"] == []


def test_a_phase_that_died_before_its_task_existed_is_not_called_never_entered(
    codex_docker, tmp_path
):
    ctx = base_ctx(collector_for(codex_docker))
    del ctx["task_id"]

    reason = build_artifact(ctx, root=tmp_path)["engineering"]["collection"]["reason"]

    assert "entered the engineering phase" in reason
    assert ctx["story_id"] in reason


def test_a_paid_missed_worker_executed_is_red_with_a_control_plane_reason(tmp_path):
    # No container was ever observed, exactly as in run 33683482667.
    collector = collector_for(FakeDocker())
    collector.capture()
    ctx = base_ctx(collector, engineering_runs=engineering_records())

    artifact = build_artifact(ctx, root=tmp_path)

    assert artifact["combination"]["worker_executed"]["status"] == CaptureStatus.MISSED.value
    assert artifact["verdict"]["paid"] is True
    assert artifact["verdict"]["status"] == run_evidence.Verdict.RED.value
    codes = [reason["code"] for reason in artifact["verdict"]["reasons"]]
    assert run_evidence.VerdictReason.WORKER_EXECUTED_MISSED.value in codes
    missed = next(
        reason
        for reason in artifact["verdict"]["reasons"]
        if reason["code"] == run_evidence.VerdictReason.WORKER_EXECUTED_MISSED.value
    )
    assert "no developer worker container was observed" in missed["detail"]
    assert missed["control_plane_reason"] == artifact["failure"]["control_plane_reason"]


def test_a_paid_missed_qa_executor_is_red_with_a_control_plane_reason(codex_docker, tmp_path):
    collector = collector_for(codex_docker)
    collector.capture()
    ctx = base_ctx(
        collector,
        task_status=TaskStatus.DONE,
        deploy_run_id="deploy-1",
        deploy_outcome=DeployOutcome.SUCCESS.value,
        final_app_status=ApplicationStatus.RUNNING.value,
        qa_run={"id": "qa-1", "result": {"qa_outcome": QAOutcome.FAILED.value}},
        engineering_runs=engineering_records(),
    )

    artifact = build_artifact(ctx, root=tmp_path)

    assert artifact["combination"]["qa_executed"]["status"] == CaptureStatus.MISSED.value
    qa_missed = next(
        reason
        for reason in artifact["verdict"]["reasons"]
        if reason["code"] == run_evidence.VerdictReason.QA_EXECUTED_MISSED.value
    )
    assert "asked for an LLM QA executor" in qa_missed["detail"]
    assert qa_missed["control_plane_reason"]["value"]["source"] == (
        run_evidence.ReasonSource.QA_RUN.value
    )


def test_a_paid_run_that_completed_with_both_executors_observed_is_green(transcripts, tmp_path):
    docker = FakeDocker(
        containers={
            DEV_CONTAINER: container_payload(
                worker_id=DEV_WORKER_ID,
                agent_type="codex",
                exit_code=0,
                transcript_source=str(transcripts),
            ),
            QA_CONTAINER: container_payload(
                worker_id=QA_WORKER_ID,
                agent_type="claude",
                exit_code=0,
                transcript_source=str(transcripts),
                worker_type="qa",
            ),
        },
        logs={DEV_CONTAINER: "done", QA_CONTAINER: "done"},
    )
    collector = collector_for(docker)
    collector.capture()
    ctx = base_ctx(
        collector,
        task_status=TaskStatus.DONE,
        deploy_run_id="deploy-1",
        deploy_outcome=DeployOutcome.SUCCESS.value,
        final_app_status=ApplicationStatus.RUNNING.value,
        qa_run={"id": "qa-1", "result": {"qa_outcome": QAOutcome.PASSED.value}},
        engineering_runs=engineering_records(
            run=Capture.captured(
                run_evidence.engineering_run_facts(
                    engineering_run_payload(status="completed", error_message=None)
                )
            )
        ),
    )

    artifact = build_artifact(ctx, root=tmp_path)

    assert artifact["failure"]["failed"] is False
    assert artifact["failure"]["control_plane_reason"]["value"]["source"] == (
        run_evidence.ReasonSource.NO_FAILURE.value
    )
    assert artifact["verdict"] == {"paid": True, "status": "green", "reasons": []}


def test_the_free_noop_suite_keeps_its_verdict_semantics(codex_docker, tmp_path):
    """A deterministic run starts no executor container, and that is not a finding."""
    collector = collector_for(FakeDocker())
    collector.capture()
    ctx = base_ctx(
        collector,
        agent_type="noop",
        qa_requires_executor=False,
        qa_agent_type_requested=None,
        qa_agent_type=None,
        task_status=TaskStatus.DONE,
        deploy_run_id="deploy-1",
        deploy_outcome=DeployOutcome.SUCCESS.value,
        final_app_status=ApplicationStatus.RUNNING.value,
        qa_run={"id": "qa-1", "result": {"qa_outcome": QAOutcome.PASSED.value}},
    )

    artifact = build_artifact(ctx, root=tmp_path)

    assert artifact["combination"]["worker_executed"]["status"] == CaptureStatus.MISSED.value
    assert artifact["combination"]["qa_executed"]["status"] == CaptureStatus.MISSED.value
    assert artifact["verdict"] == {"paid": False, "status": "green", "reasons": []}


def test_a_failed_noop_run_is_red_only_for_the_stage_it_stopped_at(codex_docker, tmp_path):
    collector = collector_for(FakeDocker())
    collector.capture()
    ctx = base_ctx(collector, agent_type="noop", qa_requires_executor=False, engineering_runs=[])

    verdict = build_artifact(ctx, root=tmp_path)["verdict"]

    assert verdict["paid"] is False
    assert verdict["status"] == run_evidence.Verdict.RED.value
    assert [reason["code"] for reason in verdict["reasons"]] == [
        run_evidence.VerdictReason.RUN_FAILED.value
    ]


def test_a_combination_with_no_agent_type_is_refused_rather_than_judged_free():
    with pytest.raises(ValueError, match="neither paid nor free"):
        run_evidence.is_paid_run({"agent_type": None})


def test_the_artifact_names_the_debug_dumps_this_run_wrote(codex_docker, tmp_path):
    ctx = base_ctx(codex_docker and collector_for(codex_docker))
    ctx["debug_dumps"] = ["debug-full-llm-engineering-20260902-101500.md"]

    artifact = build_artifact(ctx, root=tmp_path)

    assert artifact["debug_dumps"] == ["debug-full-llm-engineering-20260902-101500.md"]


# ── The QA stage that stopped run 33711527100 ───────────────────────────
#
# A paid run whose deploy reported success, whose application reached running,
# and whose QA run ended `blocked` 0.9 s later on the pre-executor reachability
# probe. The records below are the shapes `QARunResult` and `DeployRunResult`
# actually serialize.

DEPLOYED_URL = "http://198.51.100.7:8000"
QA_BLOCKED_RUN = {
    "id": "qa-deploy-poll-93440111",
    "status": "completed",
    "created_at": "2026-09-03T03:44:28+00:00",
    "updated_at": "2026-09-03T03:44:29+00:00",
    "result": {
        "qa_outcome": QAOutcome.BLOCKED.value,
        "summary": "QA could not verify the product",
        "failed_checks": [],
        "report": None,
        "qa_attempt": None,
        "deployed_url": DEPLOYED_URL,
        "error": None,
        "blocker": {
            "category": "deployed_url_unreachable",
            "attempted": "GET deployed URL before starting QA agent",
            "sent": f"GET {DEPLOYED_URL}",
            "received": "transport error: All connection attempts failed",
        },
        "telegram_probe_evidence": [],
        "state_changes": [],
    },
}
DEPLOY_SUCCESS_RUN = {
    "id": "deploy-poll-ea0bed35",
    "status": "completed",
    "created_at": "2026-09-03T03:43:10+00:00",
    "updated_at": "2026-09-03T03:44:20+00:00",
    "run_metadata": {"head_sha": "4de2a99b"},
    "result": {
        "deploy_outcome": DeployOutcome.SUCCESS.value,
        "deployed_url": DEPLOYED_URL,
        "application_id": 7,
        "deploy_fix_attempt": 0,
        "error_details": None,
        "action": None,
        "smoke_result": {"passed": True, "url": f"{DEPLOYED_URL}/health", "status_code": 200},
    },
}


def qa_stage_ctx(collector: RunEvidenceCollector, **overrides) -> dict:
    """The context a run of run 33711527100's shape leaves behind."""
    ctx = base_ctx(
        collector,
        task_status=TaskStatus.DONE,
        deploy_run_id=DEPLOY_SUCCESS_RUN["id"],
        deploy_outcome=DeployOutcome.SUCCESS.value,
        final_app_status=ApplicationStatus.RUNNING.value,
        deployed_url=DEPLOYED_URL,
        deploy_run_record=run_evidence.deploy_run_facts(DEPLOY_SUCCESS_RUN),
        qa_run=QA_BLOCKED_RUN,
        qa_run_record=run_evidence.qa_run_facts(QA_BLOCKED_RUN),
        engineering_runs=[],
    )
    ctx.update(overrides)
    return ctx


def test_a_blocked_qa_run_names_the_blocker_the_consumer_wrote(codex_docker, tmp_path):
    """`qa_outcome=blocked` is not the answer; the blocker inside the Run is."""
    collector = collector_for(codex_docker)
    collector.capture()

    artifact = build_artifact(qa_stage_ctx(collector), root=tmp_path)

    record = artifact["qa"]["run_record"]
    assert record["status"] == CaptureStatus.CAPTURED.value
    assert record["value"]["status"] == "completed"
    assert record["value"]["qa_outcome"] == QAOutcome.BLOCKED.value
    assert record["value"]["blocker"] == {
        "category": "deployed_url_unreachable",
        "attempted": "GET deployed URL before starting QA agent",
        "sent": f"GET {DEPLOYED_URL}",
        "received": "transport error: All connection attempts failed",
    }
    reason = artifact["failure"]["control_plane_reason"]["value"]
    assert reason["qa_run_record"] == record
    assert reason["deploy_run_record"]["value"]["id"] == DEPLOY_SUCCESS_RUN["id"]


def test_the_deploy_run_arrives_with_the_smoke_that_made_it_a_success(codex_docker, tmp_path):
    collector = collector_for(codex_docker)
    collector.capture()

    deployment = build_artifact(qa_stage_ctx(collector), root=tmp_path)["deployment"]

    assert deployment["deployed_url"] == DEPLOYED_URL
    assert deployment["run_record"]["value"]["deploy_outcome"] == DeployOutcome.SUCCESS.value
    assert deployment["run_record"]["value"]["head_sha"] == "4de2a99b"
    assert deployment["reachability"]["deploy_smoke"]["value"] == {
        "passed": True,
        "url": f"{DEPLOYED_URL}/health",
        "status_code": 200,
    }


def test_the_reader_can_tell_unreachable_from_rejected_and_is_told_what_is_not_here(
    codex_docker, tmp_path
):
    """AC4: which of down / up-but-unreachable / answering-something-rejected."""
    collector = collector_for(codex_docker)
    collector.capture()

    artifact = build_artifact(qa_stage_ctx(collector), root=tmp_path)
    reachability = artifact["deployment"]["reachability"]

    # QA received nothing at all, and says so in its own words.
    qa_probe = reachability["qa_probe"]["value"]
    assert qa_probe["reached_the_url"] is False
    assert qa_probe["received"] == "transport error: All connection attempts failed"
    assert qa_probe["failed_checks"] == []
    # The deploy, a minute earlier, got a 200 from the same URL.
    assert reachability["deploy_smoke"]["value"]["status_code"] == 200
    # The container's own side is not readable from the orchestrator, so the
    # artifact asks for it by name instead of leaving the question open.
    snapshot = reachability["target_host_snapshot"]
    assert snapshot["required"] is True
    assert snapshot["file"] == run_evidence.TARGET_SNAPSHOT_FILENAME
    assert "QA's own probe got no response" in snapshot["reason"]
    assert "unreachable" in snapshot["reason"]
    assert "reached_the_url" in artifact["deployment"]["note"]


def test_a_url_that_answered_something_qa_rejected_asks_for_no_target_host(codex_docker, tmp_path):
    """QA reached a verdict, so it read a response: nothing waits on the target."""
    collector = collector_for(codex_docker)
    collector.capture()
    rejected = {
        **QA_BLOCKED_RUN,
        "result": {
            **QA_BLOCKED_RUN["result"],
            "qa_outcome": QAOutcome.FAILED.value,
            "blocker": None,
            "failed_checks": [{"name": "GET /health returns 200", "detail": "got 503"}],
        },
    }
    ctx = qa_stage_ctx(
        collector,
        qa_run=rejected,
        qa_run_record=run_evidence.qa_run_facts(rejected),
        health_probe_before_undeploy={"url": DEPLOYED_URL, "status_code": 200, "body": "{}"},
    )

    reachability = build_artifact(ctx, root=tmp_path)["deployment"]["reachability"]

    assert reachability["qa_probe"]["value"]["reached_the_url"] is True
    assert reachability["qa_probe"]["value"]["failed_checks"] == [
        {"name": "GET /health returns 200", "detail": "got 503"}
    ]
    assert reachability["harness_probe"]["value"]["status_code"] == 200
    assert reachability["target_host_snapshot"]["required"] is False
    assert "got a response" in reachability["target_host_snapshot"]["reason"]


def test_a_harness_probe_that_got_nothing_is_a_stated_read_not_a_blank(codex_docker, tmp_path):
    collector = collector_for(codex_docker)
    collector.capture()
    ctx = qa_stage_ctx(
        collector,
        health_probe_error=f"health endpoint not reachable at {DEPLOYED_URL} after 5 attempts",
    )

    reachability = build_artifact(ctx, root=tmp_path)["deployment"]["reachability"]

    assert reachability["harness_probe"]["status"] == CaptureStatus.MISSED.value
    assert "no usable response" in reachability["harness_probe"]["reason"]
    assert (
        "the harness probe got no usable response"
        in (reachability["target_host_snapshot"]["reason"])
    )


def test_an_unread_qa_or_deploy_record_says_which_read_never_happened(codex_docker, tmp_path):
    collector = collector_for(codex_docker)
    collector.capture()
    ctx = qa_stage_ctx(collector)
    del ctx["qa_run_record"]
    del ctx["deploy_run_record"]

    artifact = build_artifact(ctx, root=tmp_path)

    qa_record = artifact["qa"]["run_record"]
    assert qa_record["status"] == CaptureStatus.MISSED.value
    assert QA_BLOCKED_RUN["id"] in qa_record["reason"]
    assert "did not run" in qa_record["reason"]
    deploy_record = artifact["deployment"]["run_record"]
    assert deploy_record["status"] == CaptureStatus.MISSED.value
    assert DEPLOY_SUCCESS_RUN["id"] in deploy_record["reason"]
    reachability = artifact["deployment"]["reachability"]
    assert reachability["deploy_smoke"]["status"] == CaptureStatus.MISSED.value
    assert reachability["qa_probe"]["status"] == CaptureStatus.MISSED.value
    # Nothing is asserted about a URL nobody could read a record for.
    assert reachability["target_host_snapshot"]["required"] is False


def test_a_qa_record_that_could_not_be_read_carries_the_error_that_stopped_it(
    codex_docker, tmp_path
):
    collector = collector_for(codex_docker)
    collector.capture()
    ctx = qa_stage_ctx(collector, qa_run_record_error="the terminal QA Run could not be read")
    del ctx["qa_run_record"]

    artifact = build_artifact(ctx, root=tmp_path)

    assert artifact["qa"]["run_record"] == {
        "status": CaptureStatus.MISSED.value,
        "value": None,
        "reason": "the terminal QA Run could not be read",
    }


def test_a_run_that_never_reached_qa_says_why_there_is_no_qa_record(codex_docker, tmp_path):
    collector = collector_for(codex_docker)
    collector.capture()

    artifact = build_artifact(base_ctx(collector), root=tmp_path)

    record = artifact["qa"]["run_record"]
    assert record["status"] == CaptureStatus.MISSED.value
    assert "the worker died before QA" in record["reason"]
    assert artifact["deployment"]["run_record"]["reason"].endswith(
        "this combination never reached a deploy Run"
    )


def test_the_free_noop_suite_asks_for_nothing_from_the_target_host(codex_docker, tmp_path):
    """AC6: the deterministic route's captures stay exactly what they were."""
    collector = collector_for(FakeDocker())
    collector.capture()
    ctx = qa_stage_ctx(
        collector,
        agent_type="noop",
        qa_requires_executor=False,
        qa_agent_type=None,
        qa_agent_type_requested=None,
    )

    snapshot = build_artifact(ctx, root=tmp_path)["deployment"]["reachability"][
        "target_host_snapshot"
    ]

    assert snapshot["required"] is False
    assert "free deterministic route" in snapshot["reason"]


def test_a_deploy_that_left_before_qa_does_not_claim_no_qa_run_exists(codex_docker, tmp_path):
    """The false absence: nothing looked, so nothing may be asserted."""
    collector = collector_for(codex_docker)
    collector.capture()
    ctx = qa_stage_ctx(collector)
    del ctx["qa_run"]
    del ctx["qa_run_record"]

    artifact = build_artifact(ctx, root=tmp_path)

    reason = artifact["qa"]["run_record"]["reason"]
    assert "no QA run for this story was ever read" in reason
    assert "unobserved, not absent" in reason
    assert "reached a terminal state" not in reason
    assert artifact["qa"]["run_record_source"] is None


def test_a_teardown_read_that_found_no_qa_run_may_say_so(codex_docker, tmp_path):
    collector = collector_for(codex_docker)
    collector.capture()
    ctx = qa_stage_ctx(collector, qa_run_lookup=run_evidence.QARunLookup.NONE_TERMINAL)
    del ctx["qa_run"]
    del ctx["qa_run_record"]

    reason = build_artifact(ctx, root=tmp_path)["qa"]["run_record"]["reason"]

    assert "held no terminal QA run for this story" in reason
    assert "before teardown" in reason


def test_a_teardown_read_that_failed_says_the_question_is_unread(codex_docker, tmp_path):
    collector = collector_for(codex_docker)
    collector.capture()
    ctx = qa_stage_ctx(
        collector,
        qa_run_lookup_error="the QA runs of story story-1 were read before teardown and could "
        "not be listed: ReadTimeout: timed out",
    )
    del ctx["qa_run"]
    del ctx["qa_run_record"]

    reason = build_artifact(ctx, root=tmp_path)["qa"]["run_record"]["reason"]

    assert "is unread" in reason
    assert "ReadTimeout" in reason


def test_a_qa_run_read_at_teardown_says_where_it_was_read(codex_docker, tmp_path):
    collector = collector_for(codex_docker)
    collector.capture()
    ctx = qa_stage_ctx(collector, qa_run_lookup=run_evidence.QARunLookup.TEARDOWN)

    artifact = build_artifact(ctx, root=tmp_path)

    assert artifact["qa"]["run_record_source"] == run_evidence.QARunLookup.TEARDOWN
    assert artifact["qa"]["run_record"]["value"]["qa_outcome"] == QAOutcome.BLOCKED.value


def test_reached_the_url_carries_the_basis_it_rests_on(codex_docker, tmp_path):
    """`false` is observed; `true` is an inference, and the field says which."""
    collector = collector_for(codex_docker)
    collector.capture()

    blocked = build_artifact(qa_stage_ctx(collector), root=tmp_path)
    probe = blocked["deployment"]["reachability"]["qa_probe"]["value"]
    assert probe["reached_the_url"] is False
    assert probe["reached_the_url_basis"] == run_evidence.ReachedBasis.QA_BLOCKER.value
    assert "observed" in probe["reached_the_url_note"]

    passed = {**QA_BLOCKED_RUN}
    passed["result"] = {
        **QA_BLOCKED_RUN["result"],
        "qa_outcome": QAOutcome.PASSED.value,
        "blocker": None,
    }
    ctx = qa_stage_ctx(collector, qa_run=passed, qa_run_record=run_evidence.qa_run_facts(passed))
    probe = build_artifact(ctx, root=tmp_path)["deployment"]["reachability"]["qa_probe"]["value"]
    assert probe["reached_the_url"] is True
    assert probe["reached_the_url_basis"] == run_evidence.ReachedBasis.INFERRED_FROM_OUTCOME.value
    assert "inferred" in probe["reached_the_url_note"]
    assert "Telegram" in probe["reached_the_url_note"]


def test_the_snapshot_the_run_asked_for_reports_what_became_of_it(codex_docker, tmp_path):
    collector = collector_for(codex_docker)
    collector.capture()

    never_ran = build_artifact(qa_stage_ctx(collector), root=tmp_path)
    entry = never_ran["deployment"]["reachability"]["target_host_snapshot"]
    assert entry["required"] is True
    assert entry["collection"]["status"] == CaptureStatus.MISSED.value
    assert "never attempted one" in entry["collection"]["reason"]

    collected = build_artifact(
        qa_stage_ctx(
            collector,
            target_snapshot={"file": "target-app.log", "characters": 812, "server_handle": "srv-1"},
        ),
        root=tmp_path,
    )
    entry = collected["deployment"]["reachability"]["target_host_snapshot"]
    assert entry["collection"]["value"]["file"] == run_evidence.TARGET_SNAPSHOT_FILENAME
    assert entry["reason"].endswith(
        f"beside this artifact as {run_evidence.TARGET_SNAPSHOT_FILENAME}"
    )

    failed = build_artifact(
        qa_stage_ctx(
            collector, target_snapshot_error="the target host snapshot command exited 255"
        ),
        root=tmp_path,
    )
    entry = failed["deployment"]["reachability"]["target_host_snapshot"]
    assert entry["collection"]["reason"] == "the target host snapshot command exited 255"


def test_the_collector_and_the_artifact_read_one_snapshot_predicate(codex_docker):
    """The suite asks the same question the artifact publishes and the gate reads."""
    collector = collector_for(codex_docker)
    collector.capture()

    assert run_evidence.target_snapshot_requirement(qa_stage_ctx(collector))["required"] is True
    reachable = qa_stage_ctx(
        collector,
        qa_run_record=run_evidence.qa_run_facts(
            {
                **QA_BLOCKED_RUN,
                "result": {
                    **QA_BLOCKED_RUN["result"],
                    "qa_outcome": QAOutcome.FAILED.value,
                    "blocker": None,
                },
            }
        ),
    )
    assert run_evidence.target_snapshot_requirement(reachable)["required"] is False

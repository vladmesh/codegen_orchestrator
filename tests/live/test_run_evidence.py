"""Offline regressions for the worker/QA combination evidence artifact.

These drive the collector against a fake docker, so they run with no stack:
the whole point of the artifact is that it survives the teardown race, and a
fake is the only place that race can be replayed deterministically.
"""

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

from live_harness import OwnershipManifest
import pytest
from run_evidence import (
    EVIDENCE_KIND,
    EVIDENCE_SCHEMA_VERSION,
    LOG_TAIL_MAX_CHARS,
    Capture,
    CaptureStatus,
    ContainerProbe,
    FailureKind,
    QAExercise,
    RunEvidenceCollector,
    TerminalState,
    WorkerRole,
    build_artifact,
    classify_outcome,
    combination_label,
    developer_container_prefix,
    emit_run_evidence,
    parse_docker_time,
    qa_cell,
    redact_log_tail,
    write_artifact,
)

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.deploy import DeployOutcome
from shared.contracts.queues.qa import QAOutcome

# Every test here drives the harness against fakes, so the run needs no credential
# to start — see the guard in conftest.pytest_collection_modifyitems.
pytestmark = pytest.mark.needs_no_api_credential

REPO = "live-test-llm-1a2b3c4d"
DEV_CONTAINER = f"{developer_container_prefix(REPO)}9f8e7d6c"
DEV_WORKER_ID = DEV_CONTAINER.removeprefix("worker-")
RUN_START = datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)
CODEX_REPOSITORY = "ghcr.io/org/codegen-orchestrator/worker-base-codex"


class FakeDocker:
    """A docker that can lose a container mid-run, like the real one does."""

    def __init__(self, containers: dict[str, dict], logs: dict[str, str]):
        self.containers = containers
        self.logs_by_container = logs
        self.log_reads: list[str] = []

    def remove(self, container: str) -> None:
        self.containers.pop(container, None)
        self.logs_by_container.pop(container, None)

    def probe(self) -> ContainerProbe:
        def list_workers() -> list[str]:
            return sorted(self.containers)

        def inspect(container: str) -> dict | None:
            return self.containers.get(container)

        def logs(container: str, tail: int) -> str | None:
            if container not in self.containers:
                return None
            self.log_reads.append(container)
            return self.logs_by_container[container]

        return ContainerProbe(list_workers=list_workers, inspect=inspect, logs=logs)


def container_payload(
    *,
    worker_id: str,
    agent_type: str,
    exit_code: int | None,
    transcript_source: str,
    created: datetime = RUN_START + timedelta(seconds=5),
    environment: tuple[str, ...] = (),
) -> dict:
    running = exit_code is None
    return {
        "Created": created.isoformat(),
        "Image": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
        "Config": {
            "Image": f"worker-base-{agent_type}:latest",
            "Labels": {"com.codegen.worker.id": worker_id, "com.codegen.type": "worker"},
            "Env": [f"WORKER_AGENT_TYPE={agent_type}", *environment],
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
    """A retained transcript on the host, as worker-wrapper leaves it."""
    directory = tmp_path / "worker-transcripts" / DEV_WORKER_ID
    directory.mkdir(parents=True)
    (directory / "req-1.log").write_text("--- stdout ---\n--- stderr ---\n", encoding="utf-8")
    return tmp_path / "worker-transcripts"


@pytest.fixture
def codex_docker(transcripts: Path) -> FakeDocker:
    """A Codex developer worker that exited 0 without reporting a result."""
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


def collector_for(docker: FakeDocker, *, now: datetime = RUN_START) -> RunEvidenceCollector:
    ticks = [now]

    def clock() -> datetime:
        ticks[0] = ticks[0] + timedelta(seconds=1)
        return ticks[0]

    return RunEvidenceCollector(
        developer_prefix=developer_container_prefix(REPO),
        probe=docker.probe(),
        clock=clock,
    )


def base_ctx(collector: RunEvidenceCollector, **overrides) -> dict:
    ctx = {
        "project_id": "11111111-2222-3333-4444-555555555555",
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


# ── The race the artifact exists to win ──────────────────────────────────


def test_exit_code_and_tail_survive_container_and_redis_removal(codex_docker, tmp_path):
    """The evidence outlives the cleanup that erases where it came from.

    This is the production failure: worker-manager deletes the dead worker's
    container and `_check_project_lock` deletes its Redis metadata, and by the
    time anybody looks there is nothing left to ask.
    """
    collector = collector_for(codex_docker)

    collector.capture()  # while the exited container is still on the host
    codex_docker.remove(DEV_CONTAINER)  # cleanup: container gone, meta gone
    collector.capture()

    artifact = build_artifact(base_ctx(collector), root=tmp_path)
    worker = artifact["workers"][0]
    assert worker["exit_code"] == {"status": "captured", "value": 0, "reason": None}
    assert worker["log_tail"]["status"] == "captured"
    assert "agent_exited_without_result" in worker["log_tail"]["value"]["text"]
    assert worker["agent_type_executed"]["value"] == "codex"


def test_capture_that_loses_the_race_says_so(codex_docker, tmp_path):
    """A lost race is a stated reason, never an empty field."""
    collector = collector_for(codex_docker)
    collector.observe_absent(
        DEV_WORKER_ID,
        WorkerRole.DEVELOPER,
        "docker no longer knows this container: it was removed before evidence capture",
    )

    artifact = build_artifact(base_ctx(collector), root=tmp_path)
    worker = artifact["workers"][0]
    assert worker["container_present"] is False
    assert worker["exit_code"]["status"] == CaptureStatus.MISSED.value
    assert worker["exit_code"]["value"] is None
    assert "removed before evidence capture" in worker["exit_code"]["reason"]
    assert worker["log_tail"]["reason"] == worker["exit_code"]["reason"]


def test_running_container_is_not_reported_as_exit_zero(transcripts, tmp_path):
    """A worker still running has no exit code, and the artifact says which."""
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


def test_emit_reconciles_owned_workers_that_were_never_seen(codex_docker, tmp_path):
    """A worker Redis named and docker no longer has is reported, not omitted."""
    manifest = OwnershipManifest(run_id="run-1")
    manifest.own("worker", DEV_WORKER_ID)
    manifest.own("worker", "qa-ffffffffffff")
    ctx = base_ctx(collector_for(codex_docker), manifest=manifest)

    path = emit_run_evidence(ctx, root=tmp_path)

    artifact = json.loads(path.read_text(encoding="utf-8"))
    by_id = {worker["worker_id"]: worker for worker in artifact["workers"]}
    assert by_id[DEV_WORKER_ID]["exit_code"]["value"] == 0  # captured, not overwritten
    absent = by_id["qa-ffffffffffff"]
    assert absent["role"] == WorkerRole.QA_EXECUTOR.value
    assert absent["container_present"] is False
    assert "removed before the capture reached it" in absent["exit_code"]["reason"]
    assert path.parent == tmp_path / "docs" / "e2e_results"


def test_a_container_that_never_started_is_not_reported_as_exit_zero(transcripts, tmp_path):
    """Docker says 0 for a container that never ran; that is not a clean exit."""
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


def test_probe_failure_is_recorded_not_raised(tmp_path):
    """Evidence collection never changes a matrix verdict."""

    def explode() -> list[str]:
        raise RuntimeError("docker daemon unreachable")

    collector = RunEvidenceCollector(
        developer_prefix=developer_container_prefix(REPO),
        probe=ContainerProbe(
            list_workers=explode,
            inspect=lambda name: None,
            logs=lambda name, tail: None,
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


def test_attempts_counts_every_developer_container(transcripts, tmp_path):
    """Two Codex deaths are two attempts, both attributable."""
    second = f"{developer_container_prefix(REPO)}aabbccdd"
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
    collector = collector_for(docker)
    collector.capture()
    docker.remove(DEV_CONTAINER)
    docker.containers[second] = container_payload(
        worker_id=second.removeprefix("worker-"),
        agent_type="codex",
        exit_code=1,
        transcript_source=str(transcripts),
        created=RUN_START + timedelta(seconds=120),
    )
    docker.logs_by_container[second] = "second attempt"
    collector.capture()

    artifact = build_artifact(base_ctx(collector), root=tmp_path)
    assert artifact["run"]["attempts"] == 2
    assert [worker["exit_code"]["value"] for worker in artifact["workers"]] == [0, 1]


def test_foreign_containers_are_not_attributed_to_this_run(transcripts, tmp_path):
    """Another project's worker and an earlier QA executor stay out of it."""
    docker = FakeDocker(
        containers={
            "worker-dev-other-project-11112222": container_payload(
                worker_id="dev-other-project-11112222",
                agent_type="claude",
                exit_code=0,
                transcript_source=str(transcripts),
            ),
            "worker-qa-old0old0old0": container_payload(
                worker_id="qa-old0old0old0",
                agent_type="claude",
                exit_code=0,
                transcript_source=str(transcripts),
                created=RUN_START - timedelta(minutes=5),
            ),
            "worker-qa-new1new1new1": container_payload(
                worker_id="qa-new1new1new1",
                agent_type="claude",
                exit_code=0,
                transcript_source=str(transcripts),
                created=RUN_START + timedelta(minutes=5),
            ),
        },
        logs={
            "worker-dev-other-project-11112222": "not ours",
            "worker-qa-old0old0old0": "previous combination",
            "worker-qa-new1new1new1": "ours",
        },
    )
    collector = collector_for(docker)
    collector.capture()

    workers = build_artifact(base_ctx(collector), root=tmp_path)["workers"]
    assert [worker["worker_id"] for worker in workers] == ["qa-new1new1new1"]
    assert workers[0]["role"] == WorkerRole.QA_EXECUTOR.value


def test_docker_nanosecond_timestamps_are_readable():
    """docker stamps nanoseconds; datetime reads microseconds."""
    assert parse_docker_time("2026-08-13T12:00:05.123456789Z") == datetime(
        2026, 8, 13, 12, 0, 5, 123456, tzinfo=UTC
    )
    assert parse_docker_time("2026-08-13T12:00:05Z") == RUN_START + timedelta(seconds=5)
    assert parse_docker_time("0001-01-01T00:00:00Z").year == 1
    assert parse_docker_time("not a time") is None
    assert parse_docker_time(None) is None


def test_unreadable_creation_time_keeps_the_container_and_says_so(transcripts, tmp_path):
    """An unreadable stamp is not evidence that a QA executor is foreign."""
    payload = container_payload(
        worker_id="qa-abcabcabcabc",
        agent_type="codex",
        exit_code=0,
        transcript_source=str(transcripts),
    )
    payload["Created"] = "yesterday, probably"
    docker = FakeDocker(
        containers={"worker-qa-abcabcabcabc": payload},
        logs={"worker-qa-abcabcabcabc": "qa executor log"},
    )
    collector = collector_for(docker)
    collector.capture()

    artifact = build_artifact(base_ctx(collector), root=tmp_path)
    assert [worker["worker_id"] for worker in artifact["workers"]] == ["qa-abcabcabcabc"]
    assert artifact["capture_errors"] == [
        "worker-qa-abcabcabcabc: unreadable creation time 'yesterday, probably', kept as this run's"
    ]


# ── The QA cell ──────────────────────────────────────────────────────────


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
    assert cell["executor_executed"]["value"] == "claude"


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


# ── Classification ───────────────────────────────────────────────────────


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


# ── The privacy boundary ─────────────────────────────────────────────────


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
    """Codex CLI output stays in the retained transcript, referenced by path."""
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


# ── The schema ───────────────────────────────────────────────────────────


def test_capture_cannot_be_empty_without_a_reason():
    with pytest.raises(ValueError, match="must say why"):
        Capture(CaptureStatus.MISSED)
    with pytest.raises(ValueError, match="carries no value"):
        Capture(CaptureStatus.MISSED, value=3, reason="gone")
    with pytest.raises(ValueError, match="carries no reason"):
        Capture(CaptureStatus.CAPTURED, value=3, reason="gone")


def test_artifact_schema_field_by_field(codex_docker, tmp_path):
    """Every field a post-mortem reads, asserted where it lives."""
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
    assert artifact["combination"]["qa_executed"]["value"] == "claude"

    assert artifact["project"] == {
        "id": ctx["project_id"],
        "name": REPO,
        "story_id": "story-1",
        "task_id": "task-1",
    }

    release = artifact["release"]
    assert release["deployed_sha"]["value"] == "4b220830"
    assert release["record"]["value"]["images"]["worker-base-codex"]["digest"] == "sha256:dead"
    assert release["checkout_sha"]["status"] in {status.value for status in CaptureStatus}

    run = artifact["run"]
    assert run["started_at"] == collector.started_at.isoformat()
    assert run["finished_at"] == "2026-08-13T12:05:00+00:00"
    assert run["duration_seconds"] == 299.0
    assert run["engineering_elapsed_seconds"] == 85
    assert run["attempts"] == 1
    assert run["terminal_state"] == TerminalState.STOPPED_AT_ENGINEERING.value
    assert run["failure_kind"] == FailureKind.WORKER_DID_NOT_FINISH.value
    assert run["task_status"] == TaskStatus.FAILED

    assert artifact["qa"]["state"] == QAExercise.NOT_EXERCISED.value
    assert artifact["capture_errors"] == []
    assert "redact_diagnostic" in artifact["privacy"]

    worker = artifact["workers"][0]
    assert set(worker) == {
        "worker_id",
        "role",
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

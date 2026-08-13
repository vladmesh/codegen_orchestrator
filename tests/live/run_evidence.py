"""Retained, machine-readable evidence for one worker/QA matrix combination.

Production matrix run 31688808032 at 4b220830 failed twice with Codex as the
developer worker: ``Agent exited without reporting result`` after 20-80 seconds,
and nothing retained said why. By the time anybody looked, the container was
gone, its Redis metadata was gone, and the result payload carried no Codex
output at all — worker-wrapper suppresses Codex stdout on the business path on
purpose, because CLI diagnostics can include data from the mounted session or
repository. That suppression is a privacy decision and stays.

**How a run finds its workers.** By its own label, not by having watched them.
Every dynamic worker container is stamped at creation with
``com.codegen.run.id`` — the initiating run — and with its worker id, project
and attempt (``WorkerOwnership.as_labels``). So

    docker ps -a --filter label=com.codegen.type=worker \\
              --filter label=com.codegen.run.id=<run id>

names every container this run caused, including one that has already exited
and whose ``worker:meta:<id>`` Redis record has been deleted. A pass that runs
after a worker died still reads that worker's exit code, its bounded log tail
and the path of the transcript worker-wrapper retained on the host. Nothing here
depends on a poll landing while a container happens to be alive, which is what
the previous attempt (codegen-orchestrator-1181) tried and could not make work:
a five-second poll cannot see a container that lived for one second.

What the label query cannot answer for is a container that was **removed**, not
merely killed — ``docker ps -a`` forgets those too. The run's ownership manifest
is the second source for exactly that case: a worker it names that the label
query never listed is written into the artifact as an explicit missed capture.
It can only ever add a *missed* record; every captured fact in the artifact came
from the label query.

An omitted worker would read as "nothing ran". That is the failure this module
exists to end: every worker the run created appears, either with its evidence or
with the stated reason the evidence could not be read.

Nothing here re-plumbs agent stdout into a result payload or into a service log.
The tail is the container's own log, bounded and redacted; the transcript is
referenced by path and never copied.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
import json
from pathlib import Path
import re
import subprocess

from live_harness import resolve_repo_root

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.deploy import DeployOutcome
from shared.contracts.queues.qa import QAOutcome
from shared.contracts.queues.worker import WorkerLabel
from shared.diagnostics import redact_diagnostic


def orchestrator_root() -> Path:
    """The checkout this evidence is written into and read about.

    Resolved when something actually needs it rather than at import: discovery
    needs a run id and a docker daemon and nothing else, so this module also
    imports into a test runner that mounts the tests without the checkout around
    them.
    """
    return resolve_repo_root(Path(__file__))


# The artifact is read by humans and by whatever comes next; a consumer that
# knows the version knows the field names. Bump it when a field changes meaning.
# v2: qa.executor_executed reports the executor observed on a QA container, not
#     the selector the qa-worker was configured with.
# v3: workers are discovered by `com.codegen.run.id`, so every worker record
#     carries the ownership labels it was found by and how it was discovered,
#     and the container-name/creation-window heuristics are gone.
EVIDENCE_SCHEMA_VERSION = 3
EVIDENCE_KIND = "worker_failure_attribution"

LOG_TAIL_LINES = 200
LOG_TAIL_MAX_CHARS = 12_000

# worker-manager names every worker container `worker-{worker_id}`
# (services/worker-manager/src/container_config.py) and labels it
# `com.codegen.type=worker` (manager.py `_create_worker`).
WORKER_CONTAINER_PREFIX = "worker"
WORKER_TYPE_LABEL = f"{WorkerLabel.TYPE.value}=worker"

# The container side of the transcript bind mount (container_config.py).
TRANSCRIPT_MOUNT = "/artifacts/worker-transcripts"

# Written on the deployment host by infra/scripts/pull-worker-images.sh, copied
# back by .github/workflows/deploy.yml: the digest record of the worker image
# release this host is deployed with.
RELEASE_RECORD_FILE = "deployed-worker-images.json"

# Same rule worker-wrapper redacts its transcripts with
# (packages/worker-wrapper/src/worker_wrapper/observability.py).
_SECRET_ENV_NAME = re.compile(r"(?:key|secret|token|password|credential|authorization)", re.I)


class Discovery(StrEnum):
    """How the run came to know about one worker."""

    # The run label listed the container: everything about it is readable.
    RUN_LABEL = "run_label"
    # Only the run's ownership manifest knows this worker; docker does not.
    OWNERSHIP_MANIFEST = "ownership_manifest"


# The two ways evidence is lost even with label discovery, stated in the
# artifact rather than represented by an absent worker record.
VANISHED_BEFORE_EXIT_REASON = (
    "the run label listed this container while it was still running and docker no "
    "longer lists it: it was removed between two evidence passes, so its exit code "
    "was never readable"
)
NEVER_LISTED_REASON = (
    "this run owned the worker and the run-label query never listed its container: "
    "it was removed — not merely killed — before any evidence pass, and `docker ps "
    "-a` does not remember a removed container"
)
REMOVED_BEFORE_CAPTURE_REASON = (
    "docker listed this container for the run label and no longer knows it: it was "
    "removed between the listing and its inspection"
)

PRIVACY_STATEMENT = (
    "Agent stdout/stderr never enters this artifact. The log tail is the worker "
    "container's own log — worker-wrapper's structlog output — bounded to "
    f"{LOG_TAIL_LINES} lines and {LOG_TAIL_MAX_CHARS} characters and redacted "
    "with shared.diagnostics.redact_diagnostic against every value of the "
    "container's environment whose name matches "
    "key|secret|token|password|credential|authorization, plus URL userinfo and "
    "Authorization headers. Codex CLI diagnostics stay where wrapper.py puts "
    "them: in the retained transcript on the host, referenced here by path only."
)


class CaptureStatus(StrEnum):
    """Whether one evidence field was collected, or explicitly was not."""

    CAPTURED = "captured"
    MISSED = "missed"


@dataclass(frozen=True)
class Capture:
    """One collected fact, or the stated reason it could not be collected.

    No evidence field is ever a bare empty value. "The worker produced nothing"
    and "the capture lost the race with cleanup" are different findings, and an
    artifact that cannot tell them apart is the thing this card exists to end.
    """

    status: CaptureStatus
    value: object | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status is CaptureStatus.CAPTURED:
            if self.reason is not None:
                raise ValueError("a captured value carries no reason")
            return
        if not self.reason:
            raise ValueError("a missed capture must say why it was missed")
        if self.value is not None:
            raise ValueError("a missed capture carries no value")

    @classmethod
    def captured(cls, value: object) -> Capture:
        return cls(CaptureStatus.CAPTURED, value=value)

    @classmethod
    def missed(cls, reason: str) -> Capture:
        return cls(CaptureStatus.MISSED, reason=reason)

    @property
    def is_captured(self) -> bool:
        return self.status is CaptureStatus.CAPTURED

    def as_dict(self) -> dict:
        return {"status": self.status.value, "value": self.value, "reason": self.reason}


class WorkerRole(StrEnum):
    """The role a dynamic worker container played in the combination."""

    DEVELOPER = "developer"
    QA_EXECUTOR = "qa_executor"


class RoleEvidence(StrEnum):
    """What the role of a worker was read from."""

    # `WORKER_TYPE` on the container: what worker-manager was told to create.
    CONTAINER_ENV = "container_worker_type_env"
    # The worker id itself, when no container could be inspected. QA executor
    # ids are minted `qa-{request_id[:12]}` (clients/qa_worker.py) and developer
    # ids `dev-{repo}-{request_id[:8]}` (clients/worker_spawner.py).
    WORKER_ID = "worker_id_prefix"


class TerminalState(StrEnum):
    """How far the combination got before it stopped."""

    COMPLETED = "completed"
    STOPPED_AT_SCAFFOLD = "stopped_at_scaffold"
    STOPPED_AT_ENGINEERING = "stopped_at_engineering"
    STOPPED_AT_DEPLOY = "stopped_at_deploy"
    STOPPED_AT_QA = "stopped_at_qa"


class FailureKind(StrEnum):
    """Closed classification of why the combination stopped."""

    NONE = "none"
    SCAFFOLD_FAILED = "scaffold_failed"
    ENV_CONTRACT_MISSING = "env_contract_missing"
    WORKER_DID_NOT_FINISH = "worker_did_not_finish"
    DEPLOY_RUN_MISSING = "deploy_run_missing"
    DEPLOY_FAILED = "deploy_failed"
    QA_NEVER_RAN = "qa_never_ran"
    QA_NOT_PASSED = "qa_not_passed"


class QAExercise(StrEnum):
    """Whether the QA half of the combination was actually exercised."""

    EXERCISED = "exercised"
    NOT_EXERCISED = "not_exercised"


@dataclass(frozen=True)
class ListedWorker:
    """One container the run label selected, known before anything is inspected.

    The identity comes off the labels, which the container carries from creation
    and keeps after it dies, so a listing is already an attribution even if the
    container is removed before it can be inspected.
    """

    container: str
    worker_id: str
    ownership: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ContainerProbe:
    """Every read this module makes outside the test process, injectable.

    ``list_run_workers`` answers the run-scoped label query. ``inspect`` and
    ``logs`` return ``None`` for a container docker no longer knows — that is
    reported, not raised. Any other docker failure raises: a broken probe must
    not be mistaken for a removed container.
    """

    list_run_workers: Callable[[str], list[ListedWorker]]
    inspect: Callable[[str], dict | None]
    logs: Callable[[str, int], str | None]


def _run_label_filters(run_id: str) -> list[str]:
    """The label query that defines "this run's workers"."""
    return [WORKER_TYPE_LABEL, f"{WorkerLabel.RUN.value}={run_id}"]


def docker_probe(root: Path | None = None) -> ContainerProbe:
    """The real probe: the docker CLI on the host the live harness runs on."""
    root = root if root is not None else orchestrator_root()

    def run(args: list[str], timeout: int) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout, cwd=root
        )

    def gone(result: subprocess.CompletedProcess) -> bool:
        message = f"{result.stderr}\n{result.stdout}".lower()
        return any(marker in message for marker in ("no such container", "no such object"))

    def list_run_workers(run_id: str) -> list[ListedWorker]:
        filters: list[str] = []
        for label in _run_label_filters(run_id):
            filters += ["--filter", f"label={label}"]
        result = run(
            [
                "ps",
                "-a",
                *filters,
                "--format",
                "\t".join(
                    [
                        "{{.Names}}",
                        f'{{{{.Label "{WorkerLabel.ID.value}"}}}}',
                        f'{{{{.Label "{WorkerLabel.PROJECT.value}"}}}}',
                        f'{{{{.Label "{WorkerLabel.RUN.value}"}}}}',
                        f'{{{{.Label "{WorkerLabel.ATTEMPT.value}"}}}}',
                    ]
                ),
            ],
            10,
        )
        if result.returncode != 0:
            raise RuntimeError(f"docker ps failed: {result.stderr.strip()}")
        listed = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            name, worker_id, project_id, listed_run, attempt_id = (line.split("\t") + [""] * 5)[:5]
            listed.append(
                ListedWorker(
                    container=name,
                    worker_id=worker_id or name.removeprefix(f"{WORKER_CONTAINER_PREFIX}-"),
                    ownership={
                        WorkerLabel.PROJECT.value: project_id,
                        WorkerLabel.RUN.value: listed_run,
                        WorkerLabel.ATTEMPT.value: attempt_id,
                    },
                )
            )
        return listed

    def inspect(container: str) -> dict | None:
        result = run(["inspect", "--format", "{{json .}}", container], 10)
        if result.returncode != 0:
            if gone(result):
                return None
            raise RuntimeError(f"docker inspect {container} failed: {result.stderr.strip()}")
        return json.loads(result.stdout)

    def logs(container: str, tail: int) -> str | None:
        result = run(["logs", f"--tail={tail}", container], 15)
        if result.returncode != 0:
            if gone(result):
                return None
            raise RuntimeError(f"docker logs {container} failed: {result.stderr.strip()}")
        return "\n".join(part for part in (result.stdout, result.stderr) if part.strip())

    return ContainerProbe(list_run_workers=list_run_workers, inspect=inspect, logs=logs)


def docker_sdk_probe(client) -> ContainerProbe:
    """The same probe over a docker SDK client, for a daemon reached by socket.

    Same three reads and the same label query, so a test that owns a daemon
    exercises this module's real discovery rather than a look-alike of it.
    """
    from docker.errors import NotFound  # imported here: the live harness has no docker SDK

    def list_run_workers(run_id: str) -> list[ListedWorker]:
        containers = client.containers.list(all=True, filters={"label": _run_label_filters(run_id)})
        return [
            ListedWorker(
                container=container.name,
                worker_id=container.labels[WorkerLabel.ID.value],
                ownership={
                    label.value: container.labels.get(label.value, "")
                    for label in (WorkerLabel.PROJECT, WorkerLabel.RUN, WorkerLabel.ATTEMPT)
                },
            )
            for container in containers
        ]

    def inspect(container: str) -> dict | None:
        try:
            return client.api.inspect_container(container)
        except NotFound:
            return None

    def logs(container: str, tail: int) -> str | None:
        try:
            raw = client.api.logs(container, stdout=True, stderr=True, tail=tail)
        except NotFound:
            return None
        return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)

    return ContainerProbe(list_run_workers=list_run_workers, inspect=inspect, logs=logs)


def _container_env(inspected: dict) -> dict[str, str]:
    env: dict[str, str] = {}
    for entry in inspected["Config"]["Env"] or []:
        name, _, value = entry.partition("=")
        env[name] = value
    return env


def role_from_worker_id(worker_id: str) -> WorkerRole:
    """The role a worker id alone implies, for a container nobody can inspect."""
    return WorkerRole.QA_EXECUTOR if worker_id.startswith("qa-") else WorkerRole.DEVELOPER


def _role_from_container(environment: dict[str, str], worker_id: str) -> tuple[WorkerRole, str]:
    """The role the container declares, and what that reading came from."""
    declared = environment.get("WORKER_TYPE")
    if declared == "qa":
        return WorkerRole.QA_EXECUTOR, RoleEvidence.CONTAINER_ENV.value
    if declared == "developer":
        return WorkerRole.DEVELOPER, RoleEvidence.CONTAINER_ENV.value
    return role_from_worker_id(worker_id), RoleEvidence.WORKER_ID.value


def redact_log_tail(text: str, environment: dict[str, str]) -> str:
    """Bound and redact one container log tail before it becomes evidence."""
    secrets = [
        value for name, value in environment.items() if value and _SECRET_ENV_NAME.search(name)
    ]
    redacted = redact_diagnostic(text, secrets=secrets)
    if len(redacted) > LOG_TAIL_MAX_CHARS:
        redacted = redacted[-LOG_TAIL_MAX_CHARS:]
    return redacted


def _state_evidence(inspected: dict) -> tuple[Capture, Capture]:
    """Return (state, exit_code) for one inspected container."""
    state = inspected["State"]
    running = bool(state["Running"])
    captured_state = Capture.captured(
        {
            "status": state["Status"],
            "running": running,
            "oom_killed": bool(state["OOMKilled"]),
            "started_at": state["StartedAt"],
            "finished_at": state["FinishedAt"],
            "error": state["Error"],
        }
    )
    if running:
        return captured_state, Capture.missed(
            "the container was still running when this evidence was captured"
        )
    if state["Status"] == "created":
        # Docker reports 0 for a container that never ran. Reporting that as an
        # exit code would read as a clean run of an agent that never started.
        return captured_state, Capture.missed(
            "the container was created but never started, so it has no exit code"
        )
    return captured_state, Capture.captured(int(state["ExitCode"]))


def _transcript_evidence(inspected: dict, worker_id: str) -> dict:
    """Where worker-wrapper's retained transcript for this worker lives."""
    host_dir = None
    for mount in inspected["Mounts"] or []:
        if mount.get("Destination") == TRANSCRIPT_MOUNT:
            host_dir = f"{mount['Source']}/{worker_id}"
            break
    if host_dir is None:
        return {
            "host_dir": Capture.missed(
                f"the container declares no {TRANSCRIPT_MOUNT} bind mount"
            ).as_dict(),
            "files": Capture.missed("no transcript directory to list").as_dict(),
        }
    try:
        files = Capture.captured(
            [
                {"path": str(path), "bytes": path.stat().st_size}
                for path in sorted(Path(host_dir).glob("*.log"))
            ]
        )
    except OSError as error:
        files = Capture.missed(
            f"{host_dir} is not readable from the harness host: {type(error).__name__}"
        )
    return {"host_dir": Capture.captured(host_dir).as_dict(), "files": files.as_dict()}


def capture_worker(probe: ContainerProbe, listed: ListedWorker, *, now: datetime) -> dict:
    """Collect one dynamic worker's evidence from a live or exited container.

    The worker is already attributed to this run before anything is read: it is
    here because its labels said so. What the inspection adds is how it ended.
    """
    inspected = probe.inspect(listed.container)
    if inspected is None:
        return _absent_worker(
            listed,
            role_from_worker_id(listed.worker_id),
            REMOVED_BEFORE_CAPTURE_REASON,
            now,
            discovery=Discovery.RUN_LABEL,
            role_evidence=RoleEvidence.WORKER_ID.value,
        )
    environment = _container_env(inspected)
    role, role_evidence = _role_from_container(environment, listed.worker_id)
    state, exit_code = _state_evidence(inspected)
    raw_logs = probe.logs(listed.container, LOG_TAIL_LINES)
    if raw_logs is None:
        log_tail = Capture.missed(
            "the container was removed between its inspection and its log read"
        )
    else:
        log_tail = Capture.captured(
            {
                "requested_lines": LOG_TAIL_LINES,
                "text": redact_log_tail(raw_logs, environment),
            }
        )
    agent_type = environment.get("WORKER_AGENT_TYPE")
    return {
        "worker_id": listed.worker_id,
        "role": role.value,
        "role_evidence": role_evidence,
        "discovered_by": Discovery.RUN_LABEL.value,
        "ownership_labels": dict(listed.ownership),
        "container": listed.container,
        "container_present": True,
        "agent_type_executed": (
            Capture.captured(agent_type)
            if agent_type
            else Capture.missed("the container declares no WORKER_AGENT_TYPE")
        ).as_dict(),
        "image": Capture.captured(
            {"tag": inspected["Config"]["Image"], "id": inspected["Image"]}
        ).as_dict(),
        "created_at": inspected["Created"],
        "state": state.as_dict(),
        "exit_code": exit_code.as_dict(),
        "log_tail": log_tail.as_dict(),
        "transcript": _transcript_evidence(inspected, listed.worker_id),
        "captured_at": now.isoformat(),
    }


def _absent_worker(
    listed: ListedWorker,
    role: WorkerRole,
    reason: str,
    now: datetime,
    *,
    discovery: Discovery,
    role_evidence: str,
) -> dict:
    """A worker known to have existed whose container is already gone."""
    return {
        "worker_id": listed.worker_id,
        "role": role.value,
        "role_evidence": role_evidence,
        "discovered_by": discovery.value,
        "ownership_labels": dict(listed.ownership),
        "container": listed.container,
        "container_present": False,
        "agent_type_executed": Capture.missed(reason).as_dict(),
        "image": Capture.missed(reason).as_dict(),
        "created_at": None,
        "state": Capture.missed(reason).as_dict(),
        "exit_code": Capture.missed(reason).as_dict(),
        "log_tail": Capture.missed(reason).as_dict(),
        "transcript": {
            "host_dir": Capture.missed(reason).as_dict(),
            "files": Capture.missed(reason).as_dict(),
        },
        "captured_at": now.isoformat(),
    }


def _has_exit_code(record: dict) -> bool:
    return record["exit_code"]["status"] == CaptureStatus.CAPTURED.value


def _lost_race(previous: dict, now: datetime) -> dict:
    """Downgrade a record of a running container that has since disappeared.

    What was already read stays — the state and the log tail of a container that
    was alive are true observations of this run. What is replaced is the claim
    that the exit is still pending: the container is gone, the exit code will
    never be read, and the artifact has to say that rather than keep reporting
    "still running" forever.
    """
    record = dict(previous)
    record["container_present"] = False
    record["exit_code"] = Capture.missed(VANISHED_BEFORE_EXIT_REASON).as_dict()
    if previous["log_tail"]["status"] != CaptureStatus.CAPTURED.value:
        record["log_tail"] = Capture.missed(VANISHED_BEFORE_EXIT_REASON).as_dict()
    record["captured_at"] = now.isoformat()
    return record


class RunEvidenceCollector:
    """Collects this run's dynamic worker evidence by label and keeps the best of it.

    ``capture`` asks docker for the containers labelled with this run and reads
    each one. Because the label outlives the container's death, a pass taken
    after a worker exited is as good as one taken while it ran — what a pass has
    to beat is only the *removal* of the container, which worker-manager does on
    delete. A capture that raises is recorded as a capture error rather than
    failing the run: evidence collection must never change a matrix verdict.

    ``owned_workers`` names worker ids this run is known to own from its
    ownership manifest. It is the second source, and a strictly weaker one: it
    can only add a worker as an explicit missed capture, for the case the label
    query cannot cover — a container already removed. It never contributes a
    captured fact.
    """

    def __init__(
        self,
        *,
        run_id: str,
        probe: ContainerProbe | None = None,
        owned_workers: Callable[[], list[str]] = list,
        clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        if not run_id:
            raise ValueError("run evidence is scoped to a run id; there is no unowned evidence")
        self._run_id = run_id
        self._probe = probe if probe is not None else docker_probe()
        self._owned_workers = owned_workers
        self._clock = clock
        self._started_at = clock()
        self._records: dict[str, dict] = {}
        self._errors: list[str] = []

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def started_at(self) -> datetime:
        return self._started_at

    @property
    def errors(self) -> list[str]:
        return list(self._errors)

    def capture(self) -> None:
        """Take one pass over the containers this run's label selects.

        Three things happen, in this order: what the run label lists now is
        read; what an earlier pass saw running and the label no longer lists is
        written down as a lost race; and every worker the ownership manifest
        names that the label never listed is written down as a missed capture.
        """
        try:
            listed = self._probe.list_run_workers(self._run_id)
        except Exception as error:  # noqa: BLE001 — a probe failure is evidence, not a verdict
            # A failed listing says nothing about which containers still exist,
            # so it must not be read as "everything disappeared".
            self._errors.append(f"worker container discovery failed: {error}")
            return
        for worker in listed:
            try:
                record = capture_worker(self._probe, worker, now=self._clock())
            except Exception as error:  # noqa: BLE001 — see above
                self._errors.append(f"{worker.container}: capture failed: {error}")
                continue
            self._merge(record)
        present = {worker.worker_id for worker in listed}
        self._reconcile_vanished(present)
        self._reconcile_owned(present)

    def note_error(self, message: str) -> None:
        """Record a collection failure raised outside this collector."""
        self._errors.append(message)

    def _reconcile_vanished(self, present: set[str]) -> None:
        """State the loss for a container the run label has stopped listing."""
        for worker_id, record in list(self._records.items()):
            if worker_id in present or not record["container_present"]:
                continue
            if _has_exit_code(record):
                continue
            self._records[worker_id] = _lost_race(record, self._clock())

    def _reconcile_owned(self, present: set[str]) -> None:
        """Account for every owned worker the run label does not list."""
        try:
            owned = self._owned_workers()
        except Exception as error:  # noqa: BLE001 — see capture
            self._errors.append(f"owned worker reconciliation failed: {error}")
            return
        for worker_id in owned:
            if worker_id in present or worker_id in self._records:
                continue
            self.observe_absent(worker_id, role_from_worker_id(worker_id), NEVER_LISTED_REASON)

    def observe_absent(self, worker_id: str, role: WorkerRole, reason: str) -> None:
        """Record a worker known from elsewhere whose container was never listed."""
        if worker_id in self._records:
            return
        self._records[worker_id] = _absent_worker(
            ListedWorker(
                container=f"{WORKER_CONTAINER_PREFIX}-{worker_id}",
                worker_id=worker_id,
                ownership={WorkerLabel.RUN.value: self._run_id},
            ),
            role,
            reason,
            self._clock(),
            discovery=Discovery.OWNERSHIP_MANIFEST,
            role_evidence=RoleEvidence.WORKER_ID.value,
        )

    def records(self) -> list[dict]:
        return [self._records[worker_id] for worker_id in sorted(self._records)]

    def attempts(self) -> int:
        """Developer worker containers this run went through, retries included."""
        return sum(1 for record in self._records.values() if record["role"] == WorkerRole.DEVELOPER)

    def executed_worker_agent(self) -> Capture:
        """The agent type the developer worker containers actually declared."""
        return self._executed_agent(
            WorkerRole.DEVELOPER,
            "no developer worker container was observed carrying WORKER_AGENT_TYPE",
        )

    def executed_qa_agent(self) -> Capture:
        """The agent type the QA executor containers actually declared."""
        return self._executed_agent(
            WorkerRole.QA_EXECUTOR,
            "no QA executor container was observed carrying WORKER_AGENT_TYPE",
        )

    def worker_ids(self, role: WorkerRole) -> list[str]:
        """The worker ids of one role this run has any record of."""
        return sorted(
            record["worker_id"] for record in self._records.values() if record["role"] == role
        )

    def _executed_agent(self, role: WorkerRole, nothing_observed: str) -> Capture:
        declared = sorted(
            {
                record["agent_type_executed"]["value"]
                for record in self._records.values()
                if record["role"] == role
                and record["agent_type_executed"]["status"] == CaptureStatus.CAPTURED.value
            }
        )
        if not declared:
            return Capture.missed(nothing_observed)
        if len(declared) > 1:
            return Capture.captured(declared)
        return Capture.captured(declared[0])

    def _merge(self, record: dict) -> None:
        existing = self._records.get(record["worker_id"])
        if existing is None:
            self._records[record["worker_id"]] = record
            return
        if _has_exit_code(existing) and not _has_exit_code(record):
            return
        if existing["container_present"] and not record["container_present"]:
            # The container was there and is not any more. What was read of it
            # is worth more than a bare "removed", so keep it and state the loss.
            self._records[record["worker_id"]] = _lost_race(existing, self._clock())
            return
        self._records[record["worker_id"]] = record


def classify_outcome(ctx: dict) -> tuple[TerminalState, FailureKind]:
    """Where the combination stopped, and what kind of failure stopped it."""
    contract_errors = ctx.get("env_contract_errors") or {}
    if ctx.get("scaffold_status") != ProjectStatus.ACTIVE:
        return TerminalState.STOPPED_AT_SCAFFOLD, FailureKind.SCAFFOLD_FAILED
    if "scaffold" in contract_errors:
        return TerminalState.STOPPED_AT_SCAFFOLD, FailureKind.ENV_CONTRACT_MISSING
    if ctx.get("task_status") != TaskStatus.DONE:
        return TerminalState.STOPPED_AT_ENGINEERING, FailureKind.WORKER_DID_NOT_FINISH
    if not ctx.get("deploy_run_id"):
        return TerminalState.STOPPED_AT_DEPLOY, FailureKind.DEPLOY_RUN_MISSING
    if "merged" in contract_errors:
        return TerminalState.STOPPED_AT_DEPLOY, FailureKind.ENV_CONTRACT_MISSING
    if (
        ctx.get("deploy_outcome") != DeployOutcome.SUCCESS.value
        or ctx.get("final_app_status") != ApplicationStatus.RUNNING.value
    ):
        return TerminalState.STOPPED_AT_DEPLOY, FailureKind.DEPLOY_FAILED
    qa_run = ctx.get("qa_run")
    if qa_run is None:
        return TerminalState.STOPPED_AT_QA, FailureKind.QA_NEVER_RAN
    if (qa_run.get("result") or {}).get("qa_outcome") != QAOutcome.PASSED.value:
        return TerminalState.STOPPED_AT_QA, FailureKind.QA_NOT_PASSED
    return TerminalState.COMPLETED, FailureKind.NONE


def qa_cell(ctx: dict) -> dict:
    """Report the QA half, exercised only once its worker handed QA a result.

    A QA run exists for the story only after engineering finished and the
    deploy of that work succeeded, so "a terminal QA run for this story" is the
    evidence that the worker handed something to QA. Anything short of that is
    reported as not exercised, with the reason it was not.

    The executor is reported from the QA containers this run's label selected,
    never from the selector the qa-worker was configured with: a configured
    selector is a plan, and this artifact only claims what it saw run.
    """
    collector: RunEvidenceCollector = ctx["run_evidence"]
    cell = {
        "mode": "llm_executor" if ctx.get("qa_requires_executor") else "deterministic_health",
        "executor_requested": ctx.get("qa_agent_type_requested"),
        "executor_selected": ctx.get("qa_agent_type"),
        "executor_executed": _qa_executor_evidence(ctx).as_dict(),
        "executor_workers": collector.worker_ids(WorkerRole.QA_EXECUTOR),
        "run_id": None,
        "outcome": None,
    }
    qa_run = ctx.get("qa_run")
    if qa_run is not None:
        cell["run_id"] = qa_run["id"]
        cell["outcome"] = (qa_run.get("result") or {}).get("qa_outcome")
        cell["state"] = QAExercise.EXERCISED.value
        cell["reason"] = "the worker's result was deployed and QA ran on it"
        return cell
    cell["state"] = QAExercise.NOT_EXERCISED.value
    cell["reason"] = _qa_not_exercised_reason(ctx)
    return cell


def _qa_executor_evidence(ctx: dict) -> Capture:
    """Which executor actually ran QA, judged only on container evidence."""
    collector: RunEvidenceCollector = ctx["run_evidence"]
    observed = collector.executed_qa_agent()
    if observed.is_captured:
        return observed
    if not ctx.get("qa_requires_executor"):
        return Capture.missed(
            "this run uses deterministic health-only QA, which starts no QA executor"
        )
    return Capture.missed(
        "no QA executor container of this run was observed, so the executor that ran "
        "QA is not evidenced; the qa-worker was configured to select "
        f"{ctx.get('qa_agent_type') or ctx.get('qa_agent_type_requested')!r}"
    )


def _qa_not_exercised_reason(ctx: dict) -> str:
    if ctx.get("scaffold_status") != ProjectStatus.ACTIVE:
        return f"scaffold did not reach active (status={ctx.get('scaffold_status')})"
    if ctx.get("task_status") != TaskStatus.DONE:
        return (
            "the worker died before QA: the engineering task ended "
            f"{ctx.get('task_status')}, so nothing was ever handed to QA"
        )
    if (
        ctx.get("deploy_outcome") != DeployOutcome.SUCCESS.value
        or ctx.get("final_app_status") != ApplicationStatus.RUNNING.value
    ):
        return (
            "the worker's result never reached a running deployment "
            f"(deploy_outcome={ctx.get('deploy_outcome')}, "
            f"app_status={ctx.get('final_app_status')})"
        )
    return "the deploy succeeded but no QA run for this story reached a terminal state"


def release_evidence(root: Path | None = None) -> dict:
    """The deployed SHA and the worker image digests of the release in use."""
    root = root if root is not None else orchestrator_root()
    record_path = root / RELEASE_RECORD_FILE
    try:
        record = Capture.captured(json.loads(record_path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        record = Capture.missed(
            f"{record_path} does not exist: this checkout was not deployed by deploy.yml, "
            "so no worker image release record was written for it"
        )
    except (OSError, ValueError) as error:
        record = Capture.missed(f"{record_path} is not a readable record: {type(error).__name__}")
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, cwd=root
    )
    if result.returncode == 0:
        checkout = Capture.captured(result.stdout.strip())
    else:
        checkout = Capture.missed(f"git rev-parse HEAD failed: {result.stderr.strip()}")
    deployed_sha = (
        Capture.captured(record.value["git_sha"])
        if record.is_captured and "git_sha" in record.value
        else Capture.missed("no release record names the deployed SHA")
    )
    return {
        "checkout_sha": checkout.as_dict(),
        "deployed_sha": deployed_sha.as_dict(),
        "record": record.as_dict(),
        "note": (
            "The record names the released digests the host pulled and verified. "
            "agent-matrix.yml additionally rebuilds the worker chain locally for the "
            "duration of the matrix, so the image a worker actually ran is the one "
            "under workers[].image, not necessarily a digest named here."
        ),
    }


def combination_label(ctx: dict) -> str:
    """The stable name of one worker/QA combination, used in the filename."""
    worker = ctx.get("agent_type") or "unknown"
    qa = ctx.get("qa_agent_type_requested") or (
        "health" if not ctx.get("qa_requires_executor") else "unknown"
    )
    return f"worker-{worker}-qa-{qa}"


def build_artifact(ctx: dict, *, root: Path | None = None, now: datetime | None = None) -> dict:
    """Assemble the whole artifact for one combination from the run's context."""
    root = root if root is not None else orchestrator_root()
    collector: RunEvidenceCollector = ctx["run_evidence"]
    generated_at = now if now is not None else datetime.now(tz=UTC)
    terminal_state, failure_kind = classify_outcome(ctx)
    started_at = collector.started_at
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "kind": EVIDENCE_KIND,
        "generated_at": generated_at.isoformat(),
        "combination": {
            "label": combination_label(ctx),
            "worker_requested": ctx.get("agent_type"),
            "worker_executed": collector.executed_worker_agent().as_dict(),
            "qa_requested": ctx.get("qa_agent_type_requested"),
            "qa_executed": qa_cell(ctx)["executor_executed"],
        },
        "project": {
            "id": ctx.get("project_id"),
            "name": ctx.get("project_name"),
            "story_id": ctx.get("story_id"),
            "task_id": ctx.get("task_id"),
        },
        "discovery": {
            "run_id": collector.run_id,
            "docker_filters": [f"label={label}" for label in _run_label_filters(collector.run_id)],
            "note": (
                "Workers are found by the run label their container was stamped with at "
                "creation, so one that exited — and whose Redis metadata is already "
                "deleted — is still listed and still readable. A worker the ownership "
                "manifest names that this query never listed had its container removed, "
                "and is recorded as a missed capture saying so."
            ),
        },
        "release": release_evidence(root),
        "run": {
            "id": collector.run_id,
            "started_at": started_at.isoformat(),
            "finished_at": generated_at.isoformat(),
            "duration_seconds": round((generated_at - started_at).total_seconds(), 3),
            "engineering_elapsed_seconds": ctx.get("engineering_elapsed"),
            "attempts": collector.attempts(),
            "terminal_state": terminal_state.value,
            "failure_kind": failure_kind.value,
            "task_status": ctx.get("task_status"),
            "deploy_outcome": ctx.get("deploy_outcome"),
            "app_status": ctx.get("final_app_status"),
        },
        "qa": qa_cell(ctx),
        "workers": collector.records(),
        "capture_errors": collector.errors,
        "privacy": PRIVACY_STATEMENT,
    }


def write_artifact(artifact: dict, directory: Path) -> Path:
    """Write one artifact and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = artifact["generated_at"].replace(":", "").replace("-", "")
    path = directory / f"run-evidence-{artifact['combination']['label']}-{stamp}.json"
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def emit_run_evidence(ctx: dict, *, root: Path | None = None) -> Path:
    """Take a last capture pass and write this combination's artifact.

    The last pass is what the run's dead workers are read in: they are still
    labelled, so they are still listed. Workers the run's ownership manifest
    names that no pass ever listed are reconciled in as missed captures — never
    omitted, because an omitted worker reads as "nothing ran".
    """
    collector: RunEvidenceCollector = ctx["run_evidence"]
    collector.capture()
    manifest = ctx.get("manifest")
    if manifest is not None:
        for resource in manifest.resources:
            if resource.kind != "worker":
                continue
            collector.observe_absent(
                resource.identifier,
                role_from_worker_id(resource.identifier),
                NEVER_LISTED_REASON,
            )
    artifact = build_artifact(ctx, root=root)
    return write_artifact(artifact, root / "docs" / "e2e_results")

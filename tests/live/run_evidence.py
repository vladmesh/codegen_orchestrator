"""Retained, machine-readable evidence for one worker/QA matrix combination.

Production matrix run 31688808032 at 4b220830 failed twice with Codex as the
developer worker: ``Agent exited without reporting result`` after 20-80 seconds,
and nothing retained said why. By the time anybody looked, the container was
gone, its Redis metadata was gone, and the result payload carried no Codex
output at all — worker-wrapper suppresses Codex stdout on the business path on
purpose, because CLI diagnostics can include data from the mounted session or
repository (``wrapper.py`` ~line 820). That suppression is a privacy decision
and stays.

This module collects what such a death leaves behind *before* cleanup removes
it, and writes one artifact per combination:

* the container's **exit code** and a bounded, redacted tail of the container's
  own log — that is worker-wrapper's structlog output, not agent stdout;
* the worker image the container actually ran, next to the digest record of the
  worker image release the host is deployed with;
* a pointer to the transcript worker-wrapper already wrote to the host
  bind-mount (``WORKER_TRANSCRIPT_STORAGE_PATH``), which is where a Codex exit
  can be attributed after the fact.

Nothing here re-plumbs agent stdout into a result payload or into a service
log. The tail is run evidence written beside the debug dump, bounded and
redacted; the transcript is referenced by path and never copied.

Capture runs repeatedly while engineering is still waiting, because a retry is
what destroys the previous attempt: worker-manager deletes a dead worker's
container and ``_check_project_lock`` deletes its Redis metadata when the next
worker for the same project starts. The first successful capture of a
container's exit is kept; a later pass that finds nothing cannot erase it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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
from shared.diagnostics import redact_diagnostic

ORCHESTRATOR_ROOT = resolve_repo_root(Path(__file__))

# The artifact is read by humans and by whatever comes next; a consumer that
# knows the version knows the field names. Bump it when a field changes meaning.
EVIDENCE_SCHEMA_VERSION = 1
EVIDENCE_KIND = "worker_failure_attribution"

LOG_TAIL_LINES = 200
LOG_TAIL_MAX_CHARS = 12_000

# worker-manager names every worker container `{WORKER_IMAGE_PREFIX}-{worker_id}`
# (services/worker-manager/src/config.py) and labels it `com.codegen.type=worker`.
# Developer worker ids are `dev-{repo[:20]}-{request_id[:8]}` and QA executor ids
# are `qa-{uuid[:12]}` — see worker_spawner.py and qa_worker.py.
WORKER_CONTAINER_PREFIX = "worker"
WORKER_TYPE_LABEL = "com.codegen.type=worker"
WORKER_ID_LABEL = "com.codegen.worker.id"
QA_CONTAINER_PREFIX = f"{WORKER_CONTAINER_PREFIX}-qa-"
DEVELOPER_NAME_LIMIT = 20

# The container side of the transcript bind mount (container_config.py ~line 94).
TRANSCRIPT_MOUNT = "/artifacts/worker-transcripts"

# Written on the deployment host by infra/scripts/pull-worker-images.sh, copied
# back by .github/workflows/deploy.yml: the digest record of the worker image
# release this host is deployed with.
RELEASE_RECORD_FILE = "deployed-worker-images.json"

# Same rule worker-wrapper redacts its transcripts with
# (packages/worker-wrapper/src/worker_wrapper/observability.py).
_SECRET_ENV_NAME = re.compile(r"(?:key|secret|token|password|credential|authorization)", re.I)

_DOCKER_TIME = re.compile(
    r"^(?P<head>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d+))?"
    r"(?P<zone>Z|[+-]\d{2}:?\d{2})?$"
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
class ContainerProbe:
    """Every read this module makes outside the test process, injectable.

    ``inspect`` and ``logs`` return ``None`` for a container docker no longer
    knows — that is the race this evidence is about, and it is reported, not
    raised. Any other docker failure raises: a broken probe must not be
    mistaken for a removed container.
    """

    list_workers: Callable[[], list[str]]
    inspect: Callable[[str], dict | None]
    logs: Callable[[str, int], str | None]


def docker_probe(root: Path = ORCHESTRATOR_ROOT) -> ContainerProbe:
    """The real probe: docker on the host the live harness runs on."""

    def run(args: list[str], timeout: int) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout, cwd=root
        )

    def gone(result: subprocess.CompletedProcess) -> bool:
        message = f"{result.stderr}\n{result.stdout}".lower()
        return any(marker in message for marker in ("no such container", "no such object"))

    def list_workers() -> list[str]:
        result = run(
            ["ps", "-a", "--filter", f"label={WORKER_TYPE_LABEL}", "--format", "{{.Names}}"], 10
        )
        if result.returncode != 0:
            raise RuntimeError(f"docker ps failed: {result.stderr.strip()}")
        return [name for name in result.stdout.splitlines() if name]

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

    return ContainerProbe(list_workers=list_workers, inspect=inspect, logs=logs)


def parse_docker_time(value: object) -> datetime | None:
    """Read a docker timestamp, or return None when it is not one.

    Docker stamps nanoseconds (``2026-08-13T12:00:05.123456789Z``) and
    ``datetime`` reads at most microseconds, so the fraction is truncated rather
    than rejected.
    """
    if not isinstance(value, str):
        return None
    match = _DOCKER_TIME.match(value)
    if match is None:
        return None
    fraction = match["fraction"] or ""
    stamp = match["head"] + (f".{fraction[:6]}" if fraction else "")
    zone = match["zone"] or ""
    try:
        return datetime.fromisoformat(f"{stamp}{'+00:00' if zone in ('Z', '') else zone}")
    except ValueError:
        return None


def developer_container_prefix(repo_name: str) -> str:
    """The container name prefix every developer worker of one repo carries."""
    return f"{WORKER_CONTAINER_PREFIX}-dev-{repo_name[:DEVELOPER_NAME_LIMIT]}-"


def _container_env(inspected: dict) -> dict[str, str]:
    env: dict[str, str] = {}
    for entry in inspected["Config"]["Env"] or []:
        name, _, value = entry.partition("=")
        env[name] = value
    return env


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


def capture_worker(
    probe: ContainerProbe, container: str, *, role: WorkerRole, now: datetime
) -> dict:
    """Collect one dynamic worker's evidence from a live or exited container."""
    inspected = probe.inspect(container)
    worker_id = container.removeprefix(f"{WORKER_CONTAINER_PREFIX}-")
    if inspected is None:
        return _absent_worker(
            worker_id,
            role,
            container,
            "docker no longer knows this container: it was removed before evidence capture",
            now,
        )
    labels = inspected["Config"]["Labels"] or {}
    environment = _container_env(inspected)
    state, exit_code = _state_evidence(inspected)
    raw_logs = probe.logs(container, LOG_TAIL_LINES)
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
        "worker_id": labels.get(WORKER_ID_LABEL) or worker_id,
        "role": role.value,
        "container": container,
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
        "transcript": _transcript_evidence(inspected, worker_id),
        "captured_at": now.isoformat(),
    }


def _absent_worker(
    worker_id: str, role: WorkerRole, container: str, reason: str, now: datetime
) -> dict:
    """A worker known to have existed whose container is already gone."""
    return {
        "worker_id": worker_id,
        "role": role.value,
        "container": container,
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


class RunEvidenceCollector:
    """Collects dynamic worker evidence repeatedly and keeps the best of it.

    ``capture`` is safe to call on every engineering poll: it is the only way
    attempt one survives attempt two, which deletes its container and its Redis
    metadata. A capture that raises is recorded as a capture error rather than
    failing the run — evidence collection must never change a matrix verdict.
    """

    def __init__(
        self,
        *,
        developer_prefix: str,
        probe: ContainerProbe | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self._developer_prefix = developer_prefix
        self._probe = probe if probe is not None else docker_probe()
        self._clock = clock
        self._started_at = clock()
        self._records: dict[str, dict] = {}
        self._errors: list[str] = []

    @property
    def started_at(self) -> datetime:
        return self._started_at

    @property
    def errors(self) -> list[str]:
        return list(self._errors)

    def capture(self) -> None:
        """Take one pass over this run's worker containers."""
        try:
            containers = self._probe.list_workers()
        except Exception as error:  # noqa: BLE001 — a probe failure is evidence, not a verdict
            self._errors.append(f"worker container discovery failed: {error}")
            return
        for container in containers:
            role = self._role_of(container)
            if role is None:
                continue
            try:
                record = capture_worker(self._probe, container, role=role, now=self._clock())
            except Exception as error:  # noqa: BLE001 — see above
                self._errors.append(f"{container}: capture failed: {error}")
                continue
            if role is WorkerRole.QA_EXECUTOR and not self._started_during_run(record):
                continue
            self._merge(record)

    def observe_absent(self, worker_id: str, role: WorkerRole, reason: str) -> None:
        """Record a worker known from elsewhere whose container was never seen."""
        container = f"{WORKER_CONTAINER_PREFIX}-{worker_id}"
        if container in self._records:
            return
        self._records[container] = _absent_worker(worker_id, role, container, reason, self._clock())

    def records(self) -> list[dict]:
        return [self._records[name] for name in sorted(self._records)]

    def attempts(self) -> int:
        """Developer worker containers this run went through, retries included."""
        return sum(1 for record in self._records.values() if record["role"] == WorkerRole.DEVELOPER)

    def executed_worker_agent(self) -> Capture:
        """The agent type the developer worker containers actually declared."""
        declared = sorted(
            {
                record["agent_type_executed"]["value"]
                for record in self._records.values()
                if record["role"] == WorkerRole.DEVELOPER
                and record["agent_type_executed"]["status"] == CaptureStatus.CAPTURED.value
            }
        )
        if not declared:
            return Capture.missed(
                "no developer worker container was observed carrying WORKER_AGENT_TYPE"
            )
        if len(declared) > 1:
            return Capture.captured(declared)
        return Capture.captured(declared[0])

    def _role_of(self, container: str) -> WorkerRole | None:
        if container.startswith(self._developer_prefix):
            return WorkerRole.DEVELOPER
        if container.startswith(QA_CONTAINER_PREFIX):
            return WorkerRole.QA_EXECUTOR
        return None

    def _started_during_run(self, record: dict) -> bool:
        """QA executor ids carry no project, so the run window attributes them.

        A timestamp that cannot be read is not evidence of a foreign container,
        so the container is kept and the unread stamp is reported. Over-owning
        one QA executor costs a paragraph of log tail; dropping this run's own
        executor costs the attribution this artifact exists for.
        """
        created = parse_docker_time(record["created_at"])
        if created is None:
            self._errors.append(
                f"{record['container']}: unreadable creation time "
                f"{record['created_at']!r}, kept as this run's"
            )
            return True
        return created >= self._started_at

    def _merge(self, record: dict) -> None:
        existing = self._records.get(record["container"])
        if existing is not None and _has_exit_code(existing) and not _has_exit_code(record):
            return
        self._records[record["container"]] = record


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
    """
    executed = ctx.get("qa_agent_type")
    if executed:
        executor = Capture.captured(executed)
    elif ctx.get("qa_requires_executor"):
        executor = Capture.missed("the harness never read the qa-worker's active executor")
    else:
        executor = Capture.missed(
            "this run uses deterministic health-only QA, which starts no QA executor"
        )
    cell = {
        "mode": "llm_executor" if ctx.get("qa_requires_executor") else "deterministic_health",
        "executor_requested": ctx.get("qa_agent_type_requested"),
        "executor_executed": executor.as_dict(),
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


def release_evidence(root: Path = ORCHESTRATOR_ROOT) -> dict:
    """The deployed SHA and the worker image digests of the release in use."""
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


def build_artifact(
    ctx: dict, *, root: Path = ORCHESTRATOR_ROOT, now: datetime | None = None
) -> dict:
    """Assemble the whole artifact for one combination from the run's context."""
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
        "release": release_evidence(root),
        "run": {
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


def emit_run_evidence(ctx: dict, *, root: Path = ORCHESTRATOR_ROOT) -> Path:
    """Take a last capture pass and write this combination's artifact.

    Workers the run's ownership manifest names — those the harness resolved from
    Redis — are reconciled against what was captured. One that was named and
    never seen is a lost race, and the artifact says so instead of omitting it.
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
                WorkerRole.QA_EXECUTOR
                if resource.identifier.startswith("qa-")
                else WorkerRole.DEVELOPER,
                "this run owned the worker, and no container carried it at evidence time: "
                "it was removed before the capture reached it",
            )
    artifact = build_artifact(ctx, root=root)
    return write_artifact(artifact, root / "docs" / "e2e_results")

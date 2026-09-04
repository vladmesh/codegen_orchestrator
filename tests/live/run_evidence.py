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
merely killed — ``docker ps -a`` forgets those too, and ``delete_worker``
removes rather than stops. No polling interval fixes that: a harness cannot win
a race against an asynchronous deleter. So the deleter captures instead. Before
worker-manager removes a worker's container it reads the exit code, a bounded
log tail and where the transcript was retained into a durable, run-scoped record
(``shared/contracts/worker_evidence.py``) that outlives the ``worker:meta``
deletion. That record is this module's second source, and it carries facts:
a worker created and destroyed before any pass ran still arrives here with its
exit code.

The run's ownership manifest is the third and weakest source, for a worker that
is in neither — no container and no removal record, because the capture itself
failed. It can only ever add a *missed* record, which must still name why.

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
import os
from pathlib import Path
import subprocess
from typing import TypedDict

from live_harness import resolve_repo_root

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.executor_decision import EXECUTOR_DECISION_METADATA_KEY
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.run_result import QABlockerCategory
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.deploy import DeployOutcome
from shared.contracts.queues.qa import QAOutcome
from shared.contracts.queues.worker import WorkerLabel
from shared.contracts.worker_evidence import (
    REMOVAL_LOG_TAIL_LINES,
    REMOVAL_LOG_TAIL_MAX_CHARS,
    RemovedWorkerEvidence,
    removed_worker_evidence_key,
    secret_env_values,
)
from shared.diagnostics import redact_diagnostic


def orchestrator_root() -> Path:
    """The checkout this evidence is written into and read about.

    Resolved when something actually needs it rather than at import: discovery
    needs a run id and a docker daemon and nothing else, so this module also
    imports into a test runner that mounts the tests without the checkout around
    them.
    """
    return resolve_repo_root(Path(__file__))


LIVE_EVIDENCE_OUTPUT_DIR_ENV = "LIVE_EVIDENCE_OUTPUT_DIR"


def evidence_output_directory(root: Path | None = None) -> Path:
    """Resolve the runner-owned destination before an ephemeral host disappears.

    The environment wins over any checkout, including one a caller names. On the
    stand the checkout *is* the ephemeral host: everything written under it dies
    with the machine, and the runner directory is the only place the workflow
    collects from. A caller that knows its checkout still passes it, for the
    local run where no runner directory exists.
    """
    configured = os.environ.get(LIVE_EVIDENCE_OUTPUT_DIR_ENV)
    if configured:
        return Path(configured)
    if root is None:
        root = orchestrator_root()
    return root / "docs" / "e2e_results"


# The artifact is read by humans and by whatever comes next; a consumer that
# knows the version knows the field names. Bump it when a field changes meaning.
# v2: qa.executor_executed reports the executor observed on a QA container, not
#     the selector the qa-worker was configured with.
# v3: workers are discovered by `com.codegen.run.id`, so every worker record
#     carries the ownership labels it was found by and how it was discovered,
#     and the container-name/creation-window heuristics are gone.
# v4: a worker whose container was removed is carried by the record the remover
#     wrote before removing it, so `discovered_by` may be `delete_capture` and
#     such a record carries an exit code and a log tail like any other.
# v5: task status, iteration and redacted failure metadata are retained beside
#     the worker exit/log evidence.
# v6: the failing stage and its control-plane reason are named in `failure`, the
#     engineering Run records that reason is read from are carried in
#     `engineering`, and `verdict` states red or green with the reasons for it.
# v7: the QA and deploy Run records are carried in `qa.run_record` and
#     `deployment.run_record`, `deployment.reachability` holds every read of the
#     deployed URL this run has, and a run whose deploy succeeded over a URL
#     that answered nobody asks there for a snapshot of the target host.
# v8: qa.executor_selected is a capture read from the QA Run's persisted
#     `executor_decision` — the control plane's own answer — instead of a second
#     copy of the runner's request, so requested/selected/executed are three
#     independent facts and a disagreement between them is visible.
# v9: a Product Brief scenario carries its confirmed document, coverage and
#     admission facts through the product settings and job evidence it exercised.
# v10: the Architect-owned parsed scheduled criterion and the whole admitted
#      planning roster are retained, so the job identity cannot drift from it.
# v11: `deployment.run_record` is the current deploy only and is missed when
#      unread; terminal history is `deployment.prior_attempts`; bounded repair
#      lifecycle is `deployment.settings_seed_repair`; and a story-owned
#      Engineering Run records its nullable Task foreign key explicitly. Current
#      and prior deploy facts retain credential-safe `settings_seed` outcomes.
EVIDENCE_SCHEMA_VERSION = 11
EVIDENCE_KIND = "worker_failure_attribution"

# The same bounds the remover applies to the tail it persists, so a tail read
# here and a tail read there are the same size of thing.
LOG_TAIL_LINES = REMOVAL_LOG_TAIL_LINES
LOG_TAIL_MAX_CHARS = REMOVAL_LOG_TAIL_MAX_CHARS

# worker-manager names every worker container `worker-{worker_id}`
# (services/worker-manager/src/container_config.py) and labels it
# `com.codegen.type=worker` (manager.py `_create_worker`).
WORKER_CONTAINER_PREFIX = "worker"
WORKER_TYPE_LABEL = f"{WorkerLabel.TYPE.value}=worker"

# The container side of the transcript bind mount
# (worker-manager container_config.TRANSCRIPT_MOUNT).
TRANSCRIPT_MOUNT = "/artifacts/worker-transcripts"

# The bounded, redacted snapshot the workflow takes from the *target* host when
# this artifact says the deployed URL stopped answering after a successful
# deploy. It is named here because the artifact is what asks for it: the file
# travels beside this one in the same handoff, and `scripts/stand_acceptance.py`
# refuses a paid failure that asked for it and did not get it.
TARGET_SNAPSHOT_FILENAME = "target-app.log"

# Written on the deployment host by infra/scripts/pull-worker-images.sh, copied
# back by .github/workflows/deploy.yml: the digest record of the worker image
# release this host is deployed with.
RELEASE_RECORD_FILE = "deployed-worker-images.json"


class Discovery(StrEnum):
    """How the run came to know about one worker."""

    # The run label listed the container: everything about it is readable.
    RUN_LABEL = "run_label"
    # The container is gone, and whoever removed it wrote the ending down first.
    DELETE_CAPTURE = "delete_capture"
    # Only the run's ownership manifest knows this worker; docker does not, and
    # no removal record was written for it either.
    OWNERSHIP_MANIFEST = "ownership_manifest"


# The two ways evidence is lost even with label discovery, stated in the
# artifact rather than represented by an absent worker record.
VANISHED_BEFORE_EXIT_REASON = (
    "the run label listed this container while it was still running and docker no "
    "longer lists it: it was removed between two evidence passes, so its exit code "
    "was never readable"
)
NEVER_LISTED_REASON = (
    "this run owned the worker, the run-label query never listed its container and "
    "no removal record was written for it: the container was removed — not merely "
    "killed — and the capture the remover takes before removing did not reach Redis"
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


class DeployRunRecord(TypedDict):
    """Current deploy evidence plus terminal history retained for v11."""

    current: dict | None
    current_error: str | None
    prior_attempts: list[dict]


class WorkerRole(StrEnum):
    """The role a dynamic worker container played in the combination."""

    DEVELOPER = "developer"
    QA_EXECUTOR = "qa_executor"


class RoleEvidence(StrEnum):
    """What the role of a worker was read from."""

    # `WORKER_TYPE` on the container: what worker-manager was told to create.
    CONTAINER_ENV = "container_worker_type_env"
    # The `worker_type` worker-manager held for this worker, read out of its
    # metadata by the deletion that removed it.
    DELETE_RECORD = "worker_manager_removal_record"
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
    STORY_BRANCH_NOT_AHEAD = "story_branch_not_ahead"
    DEPLOY_RUN_MISSING = "deploy_run_missing"
    DEPLOY_FAILED = "deploy_failed"
    QA_NEVER_RAN = "qa_never_ran"
    QA_NOT_PASSED = "qa_not_passed"


class QAExercise(StrEnum):
    """Whether the QA half of the combination was actually exercised."""

    EXERCISED = "exercised"
    NOT_EXERCISED = "not_exercised"


class Verdict(StrEnum):
    """What this artifact concludes about the combination it describes."""

    GREEN = "green"
    RED = "red"


class VerdictReason(StrEnum):
    """Closed vocabulary of the things that make a combination red."""

    RUN_FAILED = "run_failed"
    WORKER_EXECUTED_MISSED = "worker_executed_missed"
    QA_EXECUTED_MISSED = "qa_executed_missed"
    BRIEF_EVIDENCE_MISSED = "brief_evidence_missed"


class QARunLookup(StrEnum):
    """What the last read of this story's QA runs before teardown found."""

    # The record was read inside the QA wait, where the run was found.
    QA_WAIT = "qa_wait"
    # The phase left before the wait ran, and the teardown read found a run.
    TEARDOWN = "teardown"
    # The teardown read ran and the control plane held no terminal QA run.
    NONE_TERMINAL = "none_terminal"


class ReachedBasis(StrEnum):
    """What a `reached_the_url` value rests on, which is not one kind of thing."""

    # QA's own closed blocker category: its probe of the URL got no response.
    QA_BLOCKER = "qa_blocker_category"
    # Derived from QA reaching a product verdict, not from a read of the URL.
    INFERRED_FROM_OUTCOME = "inferred_from_qa_outcome"
    UNDETERMINED = "undetermined"


REACHED_BASIS_NOTE = {
    ReachedBasis.QA_BLOCKER: (
        "observed: the QA consumer's own pre-executor probe of the deployed URL received no "
        "response, and its transport error is in `received`"
    ),
    ReachedBasis.INFERRED_FROM_OUTCOME: (
        "inferred: QA reached a verdict about the product, which an HTTP-checked product can "
        "only be given over a response; a product QA verifies entirely over Telegram could "
        "reach the same verdict without reading the deployed URL at all"
    ),
    ReachedBasis.UNDETERMINED: (
        "neither: this QA run was stopped by something that says nothing about the deployed URL"
    ),
}


class ReasonSource(StrEnum):
    """What the control-plane reason for a stage was read from."""

    NO_FAILURE = "no_failure"
    SCAFFOLD = "scaffold"
    ENGINEERING_RUN = "engineering_run"
    STORY_BRANCH = "story_branch"
    DEPLOY_RUN = "deploy_run"
    QA_RUN = "qa_run"


# The agent type that spends nothing. Everything else on the developer side is a
# paid coding-agent subscription, and that is the whole difference between the
# free `mega-noop` suite and the paid ones.
FREE_AGENT_TYPE = "noop"

# The two distinct ways engineering evidence can be absent. They are not
# interchangeable: one says the control plane never created a Run to read, the
# other says terminal collection has not run yet and Runs may exist unread.
# Telling a reader the first in the latter case asserts something untrue about
# the run, which is the exact failure this artifact exists to remove.
ENGINEERING_PHASE_NEVER_ENTERED_REASON = (
    "no engineering Run evidence was collected for this combination: the run "
    "never entered the engineering phase, so the control plane created no Run "
    "record this artifact could have read a reason from"
)
ENGINEERING_EVIDENCE_NOT_YET_COLLECTED_REASON = (
    "no engineering Run evidence was collected for this combination: the run "
    "entered the engineering phase ({subject}), but terminal evidence collection "
    "has not run yet, so any control-plane Run records remain unread"
)


def engineering_collection_missed_reason(ctx: dict) -> str:
    """State whether engineering collection is inapplicable or not yet collected.

    The engineering phase is the only place this run creates a story and a
    task, so a context carrying neither never entered it and the control plane
    holds no Run to read. A context carrying either did enter it, and the
    terminal collection has not run yet, and Runs may well exist unread. Nothing
    new is observed to tell them apart:
    both facts are already on the context when the artifact is built.
    """
    task_id = ctx.get("task_id")
    if task_id:
        subject = (
            f"engineering task {task_id}, last observed status "
            f"{ctx.get('task_status') or 'unobserved'}"
        )
    elif ctx.get("story_id"):
        subject = f"story {ctx['story_id']}, before any engineering task was created"
    else:
        return ENGINEERING_PHASE_NEVER_ENTERED_REASON
    return ENGINEERING_EVIDENCE_NOT_YET_COLLECTED_REASON.format(subject=subject)


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

    ``removed_workers`` answers the other half of the question, for the workers
    docker cannot be asked about at all: the records worker-manager wrote before
    it removed them. It has no ``None`` case — a run with no removed workers has
    no records — and a failure to read it raises, for the same reason.
    """

    list_run_workers: Callable[[str], list[ListedWorker]]
    inspect: Callable[[str], dict | None]
    logs: Callable[[str, int], str | None]
    removed_workers: Callable[[str], list[RemovedWorkerEvidence]]


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

    return ContainerProbe(
        list_run_workers=list_run_workers,
        inspect=inspect,
        logs=logs,
        removed_workers=compose_removed_workers(root),
    )


def parse_removed_workers(payloads: list[str]) -> list[RemovedWorkerEvidence]:
    """Validate raw removal records against the contract the remover wrote them to."""
    return [RemovedWorkerEvidence.model_validate_json(payload) for payload in payloads]


def compose_removed_workers(root: Path) -> Callable[[str], list[RemovedWorkerEvidence]]:
    """Read the run's removal records out of the stack's own Redis.

    The live harness has no Redis client — it drives the stack from the host —
    so it asks the Redis service the way every other harness helper does.
    """

    def removed_workers(run_id: str) -> list[RemovedWorkerEvidence]:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "redis",
                "redis-cli",
                "HVALS",
                removed_worker_evidence_key(run_id),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=root,
        )
        if result.returncode != 0:
            raise RuntimeError(f"reading removal records failed: {result.stderr.strip()}")
        # Each record is one line: `model_dump_json` escapes every newline the
        # log tail contains, so a record never spans two lines of this output.
        return parse_removed_workers([line for line in result.stdout.splitlines() if line.strip()])

    return removed_workers


def redis_removed_workers(url: str) -> Callable[[str], list[RemovedWorkerEvidence]]:
    """Read the run's removal records straight from Redis, for a test that can."""
    import redis as redis_sdk  # imported here: the live harness has no redis client

    client = redis_sdk.Redis.from_url(url, decode_responses=True)

    def removed_workers(run_id: str) -> list[RemovedWorkerEvidence]:
        stored = client.hgetall(removed_worker_evidence_key(run_id))
        return parse_removed_workers([stored[worker_id] for worker_id in sorted(stored)])

    return removed_workers


def redis_owned_workers(url: str, run_id: str) -> Callable[[], list[str]]:
    """Name this run's workers from the metadata worker-manager still holds.

    The ownership manifest's own source, read straight from Redis by a test that
    can. It matters on exactly one path: a worker whose removal record could not
    be stored keeps its `worker:meta:<id>`, so that key is the last thing left
    that can name the worker to its run once the container is gone. It can only
    ever add a name — every fact about the worker comes from the label query or
    the removal record.
    """
    import redis as redis_sdk  # imported here: the live harness has no redis client

    client = redis_sdk.Redis.from_url(url, decode_responses=True)

    def owned_workers() -> list[str]:
        owned = []
        for key in client.scan_iter(match="worker:meta:*"):
            if client.hget(key, "run_id") == run_id:
                owned.append(key.removeprefix("worker:meta:"))
        return sorted(owned)

    return owned_workers


def docker_sdk_probe(client, removed_workers: Callable[[str], list[RemovedWorkerEvidence]]):
    """The same probe over a docker SDK client, for a daemon reached by socket.

    Same reads and the same label query, so a test that owns a daemon exercises
    this module's real discovery rather than a look-alike of it. The removal
    records come from wherever that test's Redis is, because they are not on the
    docker daemon at all.
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

    return ContainerProbe(
        list_run_workers=list_run_workers,
        inspect=inspect,
        logs=logs,
        removed_workers=removed_workers,
    )


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
    """Bound and redact one container log tail before it becomes evidence.

    The rule lives in `shared.contracts.worker_evidence`, so the tail
    worker-manager persists when it removes a container and the tail read here
    off a container that is still there are redacted against one definition of
    a secret rather than two that can drift.
    """
    redacted = redact_diagnostic(text, secrets=secret_env_values(environment))
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


def _transcript_at(host_dir: str) -> dict:
    """The retained transcript directory, and what is in it if it can be read."""
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


def _transcript_evidence(inspected: dict, worker_id: str) -> dict:
    """Where worker-wrapper's retained transcript for this worker lives."""
    for mount in inspected["Mounts"] or []:
        if mount.get("Destination") == TRANSCRIPT_MOUNT:
            return _transcript_at(f"{mount['Source']}/{worker_id}")
    return {
        "host_dir": Capture.missed(
            f"the container declares no {TRANSCRIPT_MOUNT} bind mount"
        ).as_dict(),
        "files": Capture.missed("no transcript directory to list").as_dict(),
    }


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


def _capture_of(fact) -> Capture:
    """One `RemovalFact` as this artifact's own capture vocabulary."""
    return Capture.captured(fact.value) if fact.was_read else Capture.missed(fact.missed_reason)


def _role_from_removal(evidence: RemovedWorkerEvidence) -> tuple[WorkerRole, str]:
    """The role the remover recorded, or the one the worker id implies."""
    if evidence.worker_type.was_read:
        if evidence.worker_type.value == "qa":
            return WorkerRole.QA_EXECUTOR, RoleEvidence.DELETE_RECORD.value
        if evidence.worker_type.value == "developer":
            return WorkerRole.DEVELOPER, RoleEvidence.DELETE_RECORD.value
    return role_from_worker_id(evidence.worker_id), RoleEvidence.WORKER_ID.value


def removed_worker_record(evidence: RemovedWorkerEvidence) -> dict:
    """One worker's evidence as its remover captured it, before removing it.

    The container is gone and cannot be asked anything. Everything here was read
    while it still existed, which is why this record carries an exit code and a
    log tail at all rather than the stated absence a removed container would
    otherwise be reduced to.
    """
    role, role_evidence = _role_from_removal(evidence)
    log_tail = evidence.log_tail
    if evidence.transcript_dir.was_read:
        transcript = _transcript_at(evidence.transcript_dir.value)
    else:
        transcript = {
            "host_dir": Capture.missed(evidence.transcript_dir.missed_reason).as_dict(),
            "files": Capture.missed("no transcript directory to list").as_dict(),
        }
    return {
        "worker_id": evidence.worker_id,
        "role": role.value,
        "role_evidence": role_evidence,
        "discovered_by": Discovery.DELETE_CAPTURE.value,
        "ownership_labels": {
            WorkerLabel.PROJECT.value: evidence.ownership.project_id,
            WorkerLabel.RUN.value: evidence.ownership.run_id,
            WorkerLabel.ATTEMPT.value: evidence.ownership.attempt_id,
        },
        "container": evidence.container,
        "container_present": False,
        "removed_at": evidence.removed_at,
        "delete_reason": evidence.delete_reason,
        "agent_type_executed": _capture_of(evidence.agent_type).as_dict(),
        "image": _capture_of(evidence.image).as_dict(),
        "created_at": (evidence.state.value["started_at"] if evidence.state.was_read else None),
        "state": _capture_of(evidence.state).as_dict(),
        "exit_code": _capture_of(evidence.exit_code).as_dict(),
        "log_tail": (
            Capture.captured({"requested_lines": LOG_TAIL_LINES, "text": log_tail.value})
            if log_tail.was_read
            else Capture.missed(log_tail.missed_reason)
        ).as_dict(),
        "transcript": transcript,
        "captured_at": evidence.removed_at,
    }


def _has_exit_code(record: dict) -> bool:
    return record["exit_code"]["status"] == CaptureStatus.CAPTURED.value


def _lost_race(previous: dict, now: datetime, reason: str = VANISHED_BEFORE_EXIT_REASON) -> dict:
    """Downgrade a record of a running container that has since disappeared.

    What was already read stays — the state and the log tail of a container that
    was alive are true observations of this run. What is replaced is the claim
    that the exit is still pending: the container is gone, the exit code will
    never be read, and the artifact has to say that rather than keep reporting
    "still running" forever.
    """
    record = dict(previous)
    record["container_present"] = False
    record["exit_code"] = Capture.missed(reason).as_dict()
    if previous["log_tail"]["status"] != CaptureStatus.CAPTURED.value:
        record["log_tail"] = Capture.missed(reason).as_dict()
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
        """Take one pass over everything this run's workers can still be read from.

        Four things happen, in this order: what the run label lists now is read;
        the records the remover wrote for containers that no longer exist are
        merged in; what an earlier pass saw running, the label no longer lists
        and no removal record covers is written down as a lost race; and every
        worker the ownership manifest names that neither source accounts for is
        written down as a missed capture.
        """
        listed: list[ListedWorker] | None
        try:
            listed = self._probe.list_run_workers(self._run_id)
        except Exception as error:  # noqa: BLE001 — a probe failure is evidence, not a verdict
            # A failed listing says nothing about which containers still exist,
            # so it must not be read as "everything disappeared".
            self._errors.append(f"worker container discovery failed: {error}")
            listed = None
        for worker in listed or []:
            try:
                record = capture_worker(self._probe, worker, now=self._clock())
            except Exception as error:  # noqa: BLE001 — see above
                self._errors.append(f"{worker.container}: capture failed: {error}")
                continue
            self._merge(record)
        removed = self._capture_removed()
        if listed is None:
            # The removal records are an independent source and stay true, but
            # nothing may be reconciled against a listing that never happened.
            return
        present = {worker.worker_id for worker in listed} | removed
        self._reconcile_vanished(present)
        self._reconcile_owned(present)

    def _capture_removed(self) -> set[str]:
        """Merge in what the remover captured for this run's removed containers."""
        try:
            removed = self._probe.removed_workers(self._run_id)
        except Exception as error:  # noqa: BLE001 — see capture
            self._errors.append(f"removed worker evidence read failed: {error}")
            return set()
        for evidence in removed:
            self._merge_removed(removed_worker_record(evidence))
        return {evidence.worker_id for evidence in removed}

    def _merge_removed(self, record: dict) -> None:
        """Fold one removal record into what this run already knows.

        A container that was listed and read while it still existed is the
        richer observation, so a removal record never overwrites one that
        already carries an exit code. Below that it is strictly better than
        anything else available: it is the only source that can carry an exit
        code for a container docker has forgotten.
        """
        worker_id = record["worker_id"]
        existing = self._records.get(worker_id)
        if existing is not None and _has_exit_code(existing):
            return
        if existing is None or _has_exit_code(record):
            self._records[worker_id] = record
            return
        # The remover could not read the ending either. Keep what the live
        # observation saw and state the loss in the remover's own words.
        self._records[worker_id] = _lost_race(
            existing, self._clock(), reason=record["exit_code"]["reason"]
        )

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

    def accounted_workers(self) -> set[str]:
        """The worker ids this run has evidence for, whatever that evidence says.

        Read by run-scoped cleanup, which may only delete a worker's last
        durable name — the `worker:meta:<id>` key `delete_worker` retains when a
        removal record could not be stored — once the worker is in here. A
        worker with a record is in the artifact with its ending or with the
        stated reason its ending was unreadable; a worker without one would
        simply cease to exist if its metadata were deleted now.
        """
        return set(self._records)

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
    # A task the control plane called done over a story branch that carries no
    # commit stopped at engineering, not at deploy: there is nothing to deploy,
    # and saying `deploy_run_missing` here would name the symptom the harness
    # used to wait 420 s for instead of the reason.
    if ctx.get("story_branch_error"):
        return TerminalState.STOPPED_AT_ENGINEERING, FailureKind.STORY_BRANCH_NOT_AHEAD
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

    The three executor fields are three independent facts: what the runner asked
    for, what the control plane persisted on the Run it admitted, and what a
    container was observed running. `executor_selected` used to be a second copy
    of `executor_requested`, so the two could never disagree; run 33743251165
    asked for `claude`, was admitted under `codex`, and the artifact reported
    `claude` twice.
    """
    collector: RunEvidenceCollector = ctx["run_evidence"]
    cell = {
        "mode": "llm_executor" if ctx.get("qa_requires_executor") else "deterministic_health",
        "executor_requested": ctx.get("qa_agent_type_requested"),
        "executor_selected": _qa_selected_executor(ctx).as_dict(),
        "executor_executed": _qa_executor_evidence(ctx).as_dict(),
        "executor_workers": collector.worker_ids(WorkerRole.QA_EXECUTOR),
        "run_id": None,
        "outcome": None,
        # The Run record itself, not only its outcome: an outcome of `blocked`
        # says QA stopped, and the blocker inside the record says what QA tried
        # and what did not answer.
        "run_record": _qa_run_capture(ctx).as_dict(),
        # Where that record was read: inside the QA wait, or by the last read
        # before teardown for a run that left the phase before the wait.
        "run_record_source": ctx.get("qa_run_lookup"),
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


def _qa_selected_executor(ctx: dict) -> Capture:
    """Which executor the control plane selected, read from the Run it admitted.

    The executor decision is resolved once, by the API, and persisted on the paid
    QA Run's `run_metadata` (`ExecutorDecision.as_run_metadata`); the QA consumer
    obeys it. That record is therefore the only place the *selected* executor
    exists as a fact rather than as somebody's intention — the runner's request is
    an input to it, and a consumer's configured selector is a copy of the same
    input that can be out of date.

    A decision that cannot be read is a stated missed capture, never the request:
    falling back to the request is exactly the defect, because it re-creates a
    field that agrees with the request by construction.
    """
    record = _qa_run_capture(ctx)
    if not record.is_captured:
        return Capture.missed(
            "the QA Run's persisted executor decision could not be read, so the "
            f"executor the control plane selected is not evidenced: {record.reason}"
        )
    decision = record.value.get("executor_decision")
    agent_type = decision.get("agent_type") if isinstance(decision, dict) else None
    if not agent_type:
        return Capture.missed(
            f"QA Run {record.value.get('id')!r} carries no executor decision in its "
            "run_metadata, so the executor the control plane selected is not evidenced"
        )
    return Capture.captured(agent_type)


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
    if ctx.get("story_branch_error"):
        return f"nothing was deployed for QA to run against: {ctx['story_branch_error']}"
    if (
        ctx.get("deploy_outcome") != DeployOutcome.SUCCESS.value
        or ctx.get("final_app_status") != ApplicationStatus.RUNNING.value
    ):
        return (
            "the worker's result never reached a running deployment "
            f"(deploy_outcome={ctx.get('deploy_outcome')}, "
            f"app_status={ctx.get('final_app_status')})"
        )
    # Only what was observed. A run whose deploy succeeded and whose harness
    # never looked for a QA run may well have one — run 33711527100 did, and it
    # carried the blocker this artifact exists to name — so "no QA run reached a
    # terminal state" may not be asserted unless something actually looked.
    if ctx.get("qa_run_lookup_error"):
        return (
            "the deploy succeeded and whether the control plane holds a QA run for this story "
            f"is unread: {ctx['qa_run_lookup_error']}"
        )
    if ctx.get("qa_run_lookup") == QARunLookup.NONE_TERMINAL:
        return (
            "the deploy succeeded and the control plane held no terminal QA run for this story "
            "when the harness read the runs of this story before teardown"
        )
    return (
        "the deploy succeeded and no QA run for this story was ever read: the run left the "
        "phase before anything looked, so whether a QA run exists is unobserved, not absent"
    )


def qa_run_facts(run: dict) -> dict:
    """The control-plane facts one QA Run record carries.

    Read off the Run the QA consumer wrote, not reconstructed from what the
    harness watched. A QA run that ended `blocked` never judged the product at
    all, and the blocker is the only place that says which of the closed
    `QABlockerCategory` reasons stopped it, what QA attempted, what it sent and
    what came back — the three fields `QABlocker` actually carries.
    """
    result = run.get("result") or {}
    blocker = result.get("blocker")
    return {
        "id": run.get("id"),
        "status": run.get("status"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "qa_outcome": result.get("qa_outcome"),
        "summary": result.get("summary"),
        "error": result.get("error"),
        "qa_attempt": result.get("qa_attempt"),
        # The executor this Run was admitted under, as the API resolved and
        # persisted it. `qa.executor_selected` is read from here.
        "executor_decision": (run.get("run_metadata") or {}).get(EXECUTOR_DECISION_METADATA_KEY),
        "deployed_url": result.get("deployed_url"),
        "failed_checks": result.get("failed_checks") or [],
        "blocker": (
            {
                "category": blocker.get("category"),
                "attempted": blocker.get("attempted"),
                "sent": blocker.get("sent"),
                "received": blocker.get("received"),
            }
            if blocker
            else None
        ),
    }


def deploy_run_facts(run: dict) -> dict:
    """The control-plane facts one deploy Run record carries.

    `smoke_result` is the point of reading it here: it is what the application
    answered the deploy, and a run where the deploy smoke passed and QA then
    could not reach the same URL is a different finding from one where the
    deploy never proved the application answered anything.

    `settings_seed` preserves failed-setting proof without its value, product
    response body, or write capability, so later repair evidence can explain
    why a failed deploy did not reach QA.
    """
    result = run.get("result") or {}
    return {
        "id": run.get("id"),
        "status": run.get("status"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "head_sha": (run.get("run_metadata") or {}).get("head_sha"),
        "deploy_outcome": result.get("deploy_outcome"),
        "deployed_url": result.get("deployed_url"),
        "application_id": result.get("application_id"),
        "deploy_fix_attempt": result.get("deploy_fix_attempt"),
        "error_details": result.get("error_details"),
        "settings_seed": result.get("settings_seed"),
        "action": result.get("action"),
        "smoke_result": result.get("smoke_result"),
    }


def _qa_run_capture(ctx: dict) -> Capture:
    """The QA Run record of this combination, or why there is none."""
    if ctx.get("qa_run_record_error"):
        return Capture.missed(ctx["qa_run_record_error"])
    record = ctx.get("qa_run_record")
    if record is not None:
        return Capture.captured(record)
    if ctx.get("qa_run") is not None:
        return Capture.missed(
            f"a terminal QA Run ({ctx['qa_run'].get('id')}) was observed and no record of it "
            "was read into evidence: the collection inside the QA wait did not run"
        )
    return Capture.missed(f"no QA Run record was read: {_qa_not_exercised_reason(ctx)}")


def _deploy_run_capture(ctx: dict) -> Capture:
    """The deploy Run record of this combination, or why there is none."""
    record: DeployRunRecord | None = ctx.get("deploy_run_record")
    if record is not None:
        current = record["current"]
        if current is None:
            return Capture.missed(record["current_error"] or "the current deploy Run was unread")
        return Capture.captured(current)
    if ctx.get("deploy_run_id"):
        return Capture.missed(
            f"deploy Run {ctx['deploy_run_id']} was found and no record of it was read into "
            "evidence: the wait for its outcome did not record it"
        )
    return Capture.missed(
        "no deploy Run record was read: "
        + (ctx.get("deploy_run_error") or "this combination never reached a deploy Run")
    )


def _deploy_smoke_capture(deploy: Capture) -> Capture:
    """What the application answered the deploy's own smoke check."""
    if not deploy.is_captured:
        return Capture.missed(
            f"the deploy smoke evidence is unread with its Run record: {deploy.reason}"
        )
    smoke = deploy.value.get("smoke_result")
    if smoke is None:
        return Capture.missed(
            f"deploy Run {deploy.value.get('id')} ended "
            f"{deploy.value.get('deploy_outcome')} and its result carries no smoke_result, so "
            "nothing in the control plane says what the application answered the deploy"
        )
    return Capture.captured(smoke)


def _harness_probe_capture(ctx: dict) -> Capture:
    """This suite's own HTTP read of the deployed URL from the orchestrator host."""
    probe = ctx.get("health_probe_before_undeploy")
    if probe is not None:
        return Capture.captured(probe)
    if ctx.get("health_probe_error"):
        return Capture.missed(
            "the harness read the deployed URL from the orchestrator host and got no usable "
            f"response: {ctx['health_probe_error']}"
        )
    return Capture.missed(
        "the harness ran no health probe of its own: it probes only a successful deploy of a "
        f"running application (deploy_outcome={ctx.get('deploy_outcome')}, "
        f"app_status={ctx.get('final_app_status')})"
    )


def _qa_probe_capture(qa_run: Capture) -> Capture:
    """What QA itself got from the deployed URL, as its own Run record states it.

    `reached_the_url` is the distinction the whole section exists for, and it
    carries the basis it rests on rather than reading as one kind of fact. Only
    `false` is observed: QA's own closed blocker category says its probe of the
    deployed URL got nothing. `true` is an inference from a product verdict —
    sound for an HTTP-checked product, and an overclaim for one QA verifies
    entirely over Telegram without ever reading the deployed URL — so the field
    says so instead of presenting both as the same reading. `null` asserts
    nothing about a run stopped by something else.
    """
    if not qa_run.is_captured:
        return Capture.missed(
            f"what QA got from the deployed URL is unread with its Run record: {qa_run.reason}"
        )
    facts = qa_run.value
    blocker = facts.get("blocker") or {}
    outcome = facts.get("qa_outcome")
    reached: bool | None = None
    basis = ReachedBasis.UNDETERMINED
    if blocker.get("category") == QABlockerCategory.DEPLOYED_URL_UNREACHABLE.value:
        reached = False
        basis = ReachedBasis.QA_BLOCKER
    elif outcome in {QAOutcome.PASSED.value, QAOutcome.FAILED.value, QAOutcome.EXHAUSTED.value}:
        reached = True
        basis = ReachedBasis.INFERRED_FROM_OUTCOME
    return Capture.captured(
        {
            "qa_outcome": outcome,
            "reached_the_url": reached,
            "reached_the_url_basis": basis.value,
            "reached_the_url_note": REACHED_BASIS_NOTE[basis],
            "deployed_url": facts.get("deployed_url"),
            "blocker_category": blocker.get("category") or None,
            "attempted": blocker.get("attempted"),
            "sent": blocker.get("sent"),
            "received": blocker.get("received"),
            "failed_checks": facts.get("failed_checks") or [],
        }
    )


def target_snapshot_requirement(ctx: dict) -> dict:
    """Whether this run owes a target-host snapshot, and why — for the collector.

    The suite asks this *inside the phase*, before its own teardown removes the
    deployment's containers, and the artifact publishes the same answer. One
    predicate, so the collection and the artifact that reports it cannot
    disagree about which runs owe a snapshot.
    """
    return _target_snapshot_requirement(ctx, _qa_probe_capture(_qa_run_capture(ctx)))


def _target_snapshot_capture(ctx: dict, *, required: bool) -> Capture:
    """What became of the snapshot this run asked the suite to take."""
    if not required:
        return Capture.missed("this run asked for no snapshot of the target host")
    if ctx.get("target_snapshot_error"):
        return Capture.missed(ctx["target_snapshot_error"])
    snapshot = ctx.get("target_snapshot")
    if snapshot is not None:
        return Capture.captured(snapshot)
    return Capture.missed(
        "this run asked for a snapshot of the target host and the suite never attempted one: "
        "the collection that takes it, before teardown removes the deployment, did not run"
    )


def _target_snapshot_requirement(ctx: dict, qa_probe: Capture) -> dict:
    """Whether the rest of the answer is only readable on the target host.

    A deploy that reported success and a deployed URL that then answered nobody
    is the one case this artifact cannot close from the orchestrator side: the
    application container's own state and log tail live on the target machine,
    which the workflow deletes minutes later. The requirement is stated here, in
    the artifact, so the workflow collects on the artifact's own account and the
    admission refuses a paid failure that asked and was not answered.
    """
    entry = {"required": False, "file": TARGET_SNAPSHOT_FILENAME, "reason": ""}
    if not is_paid_run(ctx):
        entry["reason"] = (
            "this is the free deterministic route, whose captures are what they have always "
            "been: it asks for nothing from the target host"
        )
        return entry
    if ctx.get("deploy_outcome") != DeployOutcome.SUCCESS.value:
        entry["reason"] = (
            f"the deploy did not report success (deploy_outcome={ctx.get('deploy_outcome')}), so "
            "an unanswered deployed URL is already accounted for by the deploy stage"
        )
        return entry
    findings: list[str] = []
    if qa_probe.is_captured and qa_probe.value["reached_the_url"] is False:
        findings.append(f"QA's own probe got no response ({qa_probe.value['received']})")
    if ctx.get("health_probe_error"):
        findings.append(f"the harness probe got no usable response ({ctx['health_probe_error']})")
    if ctx.get("final_app_status") != ApplicationStatus.RUNNING.value:
        findings.append(
            f"the application was last read as {ctx.get('final_app_status')}, not running"
        )
    if not findings:
        entry["reason"] = (
            "the deploy reported success and every read of the deployed URL recorded here got a "
            "response, so nothing is waiting on the target host"
        )
        return entry
    entry["required"] = True
    entry["reason"] = (
        "the deploy reported success and " + "; ".join(findings) + " — whether the application "
        "container was down, up but unreachable, or answering something QA rejected is readable "
        "only on the target host, so the suite takes that snapshot before its own teardown "
        f"removes the deployment and writes it beside this artifact as {TARGET_SNAPSHOT_FILENAME}"
    )
    return entry


REACHABILITY_NOTE = (
    "Three fields separate 'the app was down', 'the app was up but unreachable from the "
    "orchestrator' and 'the app answered something QA rejected'. `deploy_smoke` is what the "
    "application answered the deploy itself. `harness_probe` is this suite's own HTTP read of "
    "the same URL from the orchestrator host, with the status code and a bounded body slice. "
    "`qa_probe` is what the QA consumer got: `reached_the_url=false` carries the blocker's own "
    "transport error, so QA received nothing, while `reached_the_url=true` means QA read a "
    "response and judged its content, which `failed_checks` then names. The container's own "
    f"side is not readable from the orchestrator: it is {TARGET_SNAPSHOT_FILENAME}, which the "
    "suite takes from the target host — before its own teardown removes those containers — "
    "whenever `target_host_snapshot.required` is true, and whose collection is reported under "
    "`target_host_snapshot.collection`."
)


def _target_snapshot_entry(ctx: dict, qa_probe: Capture) -> dict:
    """The requirement and what became of it, in the one place a reader looks."""
    entry = _target_snapshot_requirement(ctx, qa_probe)
    entry["collection"] = _target_snapshot_capture(ctx, required=entry["required"]).as_dict()
    return entry


def deployment_evidence(ctx: dict) -> dict:
    """The deploy Run, and every read of the deployed URL this run holds."""
    deploy_run = _deploy_run_capture(ctx)
    qa_probe = _qa_probe_capture(_qa_run_capture(ctx))
    record: DeployRunRecord | None = ctx.get("deploy_run_record")
    return {
        # This remains available when collecting the Run payload itself failed:
        # the artifact can then distinguish an absent deploy from an observed,
        # but unread, current Run.
        "run_id": ctx.get("deploy_run_id"),
        "deployed_url": ctx.get("deployed_url"),
        "run_record": deploy_run.as_dict(),
        "prior_attempts": record["prior_attempts"] if record is not None else [],
        "settings_seed_repair": {
            "run_ids": ctx.get("settings_seed_repair_run_ids") or [],
            "attempts": ctx.get("settings_seed_repair_attempts") or [],
            "error": ctx.get("settings_seed_repair_error"),
            "run_status": ctx.get("settings_seed_repair_run_status"),
            "story_status": ctx.get("settings_seed_repair_story_status"),
            "candidate_timestamp_errors": ctx.get("deploy_run_candidate_timestamp_errors") or [],
        },
        "reachability": {
            "deploy_smoke": _deploy_smoke_capture(deploy_run).as_dict(),
            "harness_probe": _harness_probe_capture(ctx).as_dict(),
            "qa_probe": qa_probe.as_dict(),
            "target_host_snapshot": _target_snapshot_entry(ctx, qa_probe),
        },
        "note": REACHABILITY_NOTE,
    }


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
            "A run that rebuilds the worker chain locally leaves the image a worker "
            "actually ran under workers[].image, which is then not necessarily a "
            "digest named here."
        ),
    }


def is_paid_run(ctx: dict) -> bool:
    """Whether this combination spent a paid coding-agent subscription.

    Read from the developer agent the project was created with, which is the
    fact that decides it: `noop` is the free deterministic route, anything else
    is a real agent on a real subscription. There is no default — a combination
    with no agent type cannot be judged either way, and guessing "free" would
    quietly switch off the paid verdict rules below.
    """
    agent_type = ctx.get("agent_type")
    if not agent_type:
        raise ValueError("a combination with no agent type is neither paid nor free")
    return agent_type != FREE_AGENT_TYPE


def engineering_run_facts(run: dict) -> dict:
    """The control-plane facts one engineering Run record carries.

    Read off the Run itself rather than reconstructed from anything the harness
    watched: the status the control plane concluded, the message it wrote, the
    stop reason it recorded in `run_metadata`, and the executor decision that
    Run was dispatched under.
    """
    metadata = run.get("run_metadata") or {}
    return {
        "id": run.get("id"),
        "status": run.get("status"),
        "error_message": run.get("error_message"),
        "stop_reason": metadata.get("stop_reason"),
        "agent_limit_seconds": metadata.get("agent_limit_seconds"),
        "executor_decision": metadata.get("executor_decision"),
        "result": run.get("result"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
    }


def engineering_run_record(
    *,
    run_id: str,
    task_id: str | None,
    run: Capture,
    admission: Capture,
    executor_diagnostics: Capture,
) -> dict:
    """One engineering Run of this combination, with what was in force for it.

    Three facts, each collected or explicitly not: the Run record, the
    work-admission outcome that allowed the Run to exist, and the executor
    diagnostics snapshot in force at the moment it was read. A piece that could
    not be read is a stated missed capture; none of them is ever simply absent.
    """
    return {
        "run_id": run_id,
        "task_id": task_id,
        "run": run.as_dict(),
        "admission": admission.as_dict(),
        "executor_diagnostics": executor_diagnostics.as_dict(),
    }


def engineering_evidence(ctx: dict) -> dict:
    """Every engineering Run of this combination, or why there are none."""
    if ctx.get("engineering_evidence_error"):
        collection = Capture.missed(ctx["engineering_evidence_error"])
        records = ctx.get("engineering_runs") or []
    elif "engineering_runs" not in ctx:
        collection = Capture.missed(engineering_collection_missed_reason(ctx))
        records = []
    else:
        records = ctx["engineering_runs"]
        collection = Capture.captured({"runs_read": len(records)})
    return {"collection": collection.as_dict(), "runs": records}


def _engineering_reason_entry(record: dict) -> dict:
    """One Run's contribution to the engineering stage's control-plane reason."""
    run = record["run"]
    entry: dict = {"run_id": record["run_id"], "task_id": record["task_id"]}
    if run["status"] == CaptureStatus.CAPTURED.value:
        facts = run["value"]
        entry |= {
            "run_status": facts["status"],
            "error_message": facts["error_message"],
            "stop_reason": facts["stop_reason"],
            "executor_decision": facts["executor_decision"],
        }
    else:
        entry["run_unread"] = run["reason"]
    admission = record["admission"]
    if admission["status"] == CaptureStatus.CAPTURED.value:
        entry["admission_outcome"] = admission["value"].get("outcome")
        entry["admission_reason"] = admission["value"].get("reason")
    else:
        entry["admission_unread"] = admission["reason"]
    return entry


def _engineering_reason(ctx: dict) -> Capture:
    """Why the control plane stopped this combination at engineering.

    The engineering Run is the only place that answer exists: the task status
    says the work ended, and the Run says what ended it. When no Run could be
    read the artifact says exactly that instead of leaving the question blank —
    a reader with only this file still learns that the reason was unreadable
    and why, which is a different finding from "the worker simply failed".
    """
    section = engineering_evidence(ctx)
    task_id = ctx.get("task_id")
    diagnostics = (ctx.get("task_diagnostics") or {}).get(task_id) or {}
    if section["collection"]["status"] == CaptureStatus.MISSED.value:
        return Capture.missed(
            "the engineering stage stopped this run and its Run records could not "
            f"be read, so no control-plane reason is available: {section['collection']['reason']}"
        )
    if not section["runs"]:
        return Capture.missed(
            f"the engineering task ended {ctx.get('task_status')} and the control plane "
            "holds no engineering Run for it, so there is no record to read a reason from"
        )
    return Capture.captured(
        {
            "source": ReasonSource.ENGINEERING_RUN.value,
            "task_status": ctx.get("task_status"),
            "task_failure_metadata": diagnostics.get("failure_metadata"),
            "task_iteration": diagnostics.get("current_iteration"),
            "runs": [_engineering_reason_entry(record) for record in section["runs"]],
        }
    )


def _env_contract_errors(ctx: dict) -> dict[str, str]:
    """The environment-contract failures of this run, phase by phase, with what failed.

    The phase key alone says which probe failed and not why; for a paid run that
    stops at scaffold or deploy that key *is* the whole control-plane reason, so
    the message travels with it. It is a probe's own text, so it goes through the
    same redaction every other retained diagnostic does.
    """
    errors = ctx.get("env_contract_errors") or {}
    secrets = secret_env_values(dict(os.environ))
    return {
        phase: redact_diagnostic(message, secrets=secrets)
        for phase, message in sorted(errors.items())
    }


def control_plane_reason(
    ctx: dict, terminal_state: TerminalState, failure_kind: FailureKind
) -> Capture:
    """The control plane's own account of why the run ended where it did."""
    if terminal_state is TerminalState.COMPLETED:
        return Capture.captured(
            {"source": ReasonSource.NO_FAILURE.value, "detail": "the combination completed"}
        )
    if terminal_state is TerminalState.STOPPED_AT_SCAFFOLD:
        return Capture.captured(
            {
                "source": ReasonSource.SCAFFOLD.value,
                "scaffold_status": ctx.get("scaffold_status"),
                "env_contract_errors": _env_contract_errors(ctx),
            }
        )
    if failure_kind is FailureKind.STORY_BRANCH_NOT_AHEAD:
        return Capture.captured(
            {
                "source": ReasonSource.STORY_BRANCH.value,
                "story_id": ctx.get("story_id"),
                "branch": ctx.get("story_branch"),
                "compare": ctx.get("story_branch_compare"),
                "detail": ctx["story_branch_error"],
                # The engineering Runs are kept alongside it: they are what
                # called this task done without a commit, and a reader chasing
                # that needs both halves in one place.
                "engineering": _engineering_reason(ctx).as_dict(),
            }
        )
    if terminal_state is TerminalState.STOPPED_AT_ENGINEERING:
        return _engineering_reason(ctx)
    if terminal_state is TerminalState.STOPPED_AT_DEPLOY:
        return Capture.captured(
            {
                "source": ReasonSource.DEPLOY_RUN.value,
                "deploy_run_id": ctx.get("deploy_run_id"),
                "deploy_outcome": ctx.get("deploy_outcome"),
                "deploy_error_details": ctx.get("deploy_error_details"),
                "app_status": ctx.get("final_app_status"),
                "env_contract_errors": _env_contract_errors(ctx),
                # The deploy Run itself, for the same reason the engineering
                # Runs travel with the story-branch reason: the outcome says the
                # stage ended, the record says what it did.
                "deploy_run_record": _deploy_run_capture(ctx).as_dict(),
            }
        )
    qa_run = ctx.get("qa_run") or {}
    return Capture.captured(
        {
            "source": ReasonSource.QA_RUN.value,
            "qa_run_id": qa_run.get("id"),
            "qa_outcome": (qa_run.get("result") or {}).get("qa_outcome"),
            "detail": _qa_not_exercised_reason(ctx) if not qa_run else "the QA run did not pass",
            # `qa_outcome=blocked` says QA stopped and nothing else. The record
            # carries the blocker QA wrote — its category, what QA attempted and
            # what came back — and the deploy that handed QA this deployment,
            # which is the other half of a QA stage that failed on reachability.
            "qa_run_record": _qa_run_capture(ctx).as_dict(),
            "deploy_run_record": _deploy_run_capture(ctx).as_dict(),
        }
    )


def failure_summary(
    ctx: dict, terminal_state: TerminalState, failure_kind: FailureKind, reason: Capture
) -> dict:
    """The failing stage and the control-plane reason for it, in one place.

    A reader with only this artifact answers both questions here: where the run
    stopped, and why the control plane says it stopped. No ssh to a host that no
    longer exists, and no inference from an absent field.
    """
    return {
        "failed": terminal_state is not TerminalState.COMPLETED,
        "stage": terminal_state.value,
        "failure_kind": failure_kind.value,
        "control_plane_reason": reason.as_dict(),
    }


def _brief_not_required_capture(name: str) -> Capture:
    return Capture.missed(f"this is not a Product Brief scenario, so {name} is not required")


def _brief_confirmed_capture(ctx: dict) -> Capture:
    brief_id = ctx.get("brief_id")
    brief = ctx.get("brief_read")
    if not isinstance(brief_id, str) or not brief_id:
        return Capture.missed("the Product Brief id was not retained by the scenario")
    if not isinstance(brief, dict):
        return Capture.missed(f"Product Brief {brief_id!r} was not read from the API")
    if brief.get("id") != brief_id:
        return Capture.missed(
            f"the Product Brief API read named {brief.get('id')!r}, not expected {brief_id!r}"
        )
    if not brief.get("confirmed_at"):
        return Capture.missed(f"Product Brief {brief_id!r} has no confirmed_at fact")
    content = brief.get("content")
    if not isinstance(content, dict):
        return Capture.missed(f"confirmed Product Brief {brief_id!r} carries no content object")
    return Capture.captured(
        {"id": brief_id, "confirmed_at": brief["confirmed_at"], "content": content}
    )


def _brief_coverage_capture(ctx: dict, confirmed: Capture) -> Capture:
    if not confirmed.is_captured:
        return Capture.missed(
            f"coverage is unread because confirmation is unread: {confirmed.reason}"
        )
    rows = ctx.get("brief_coverage")
    if not isinstance(rows, list):
        return Capture.missed("Product Brief coverage rows were not read from the API")
    brief_id = confirmed.value["id"]
    must_requirements = confirmed.value["content"].get("must_requirements")
    if not isinstance(must_requirements, list):
        return Capture.missed(f"confirmed Product Brief {brief_id!r} has no must-requirements list")
    requirement_ids = {
        requirement.get("id")
        for requirement in must_requirements
        if isinstance(requirement, dict) and isinstance(requirement.get("id"), str)
    }
    if not requirement_ids:
        return Capture.missed(
            f"confirmed Product Brief {brief_id!r} names no readable requirements"
        )

    retained: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            return Capture.missed("a Product Brief coverage row was not an object")
        if row.get("brief_id") != brief_id:
            return Capture.missed(
                f"a coverage row belongs to {row.get('brief_id')!r}, not Product Brief {brief_id!r}"
            )
        requirement_id = row.get("requirement_id")
        task_id = row.get("task_id")
        if (
            not isinstance(row.get("id"), int)
            or not isinstance(requirement_id, str)
            or not requirement_id
        ):
            return Capture.missed(
                "a Product Brief coverage row lacks its durable id or requirement id"
            )
        if not isinstance(task_id, str) or not task_id:
            return Capture.missed(
                f"Product Brief requirement {requirement_id!r} is not covered by a planning task"
            )
        retained.append(
            {
                "id": row["id"],
                "brief_id": brief_id,
                "requirement_id": requirement_id,
                "task_id": task_id,
            }
        )
    covered_ids = {row["requirement_id"] for row in retained}
    if covered_ids != requirement_ids:
        return Capture.missed(
            f"Product Brief {brief_id!r} coverage ids {sorted(covered_ids)!r} do not equal its "
            f"must-requirements {sorted(requirement_ids)!r}"
        )
    return Capture.captured(retained)


def _brief_admission_capture(  # noqa: PLR0911 - each unread durable fact has its own reason
    ctx: dict, confirmed: Capture, coverage: Capture
) -> Capture:
    if not confirmed.is_captured:
        return Capture.missed(
            f"admission is unread because confirmation is unread: {confirmed.reason}"
        )
    admission = ctx.get("brief_admission")
    brief_id = confirmed.value["id"]
    if not isinstance(admission, dict):
        return Capture.missed(f"Product Brief {brief_id!r} admission was not retained")
    if admission.get("brief_id") != brief_id:
        return Capture.missed(
            f"admission names Product Brief {admission.get('brief_id')!r}, not {brief_id!r}"
        )
    admitted_at = admission.get("coverage_admitted_at")
    planning_attempt_id = admission.get("planning_attempt_id")
    released_task_ids = admission.get("released_task_ids")
    if not isinstance(admitted_at, str) or not admitted_at:
        return Capture.missed(f"Product Brief {brief_id!r} admission has no coverage_admitted_at")
    if not isinstance(released_task_ids, list) or not all(
        isinstance(task_id, str) and task_id for task_id in released_task_ids
    ):
        return Capture.missed(
            f"Product Brief {brief_id!r} admission has no readable released task ids"
        )
    if len(set(released_task_ids)) != len(released_task_ids):
        return Capture.missed(f"Product Brief {brief_id!r} admission repeats a released task id")
    planned_task_ids = ctx.get("brief_plan_task_ids")
    if planned_task_ids != released_task_ids:
        return Capture.missed(
            f"Product Brief {brief_id!r} admission released {released_task_ids!r}, not the "
            f"current planning roster {planned_task_ids!r}"
        )
    if not isinstance(planning_attempt_id, str) or not planning_attempt_id:
        return Capture.missed(f"Product Brief {brief_id!r} admission has no planning attempt id")
    planned_tasks = ctx.get("brief_planned_tasks")
    if not isinstance(planned_tasks, list):
        return Capture.missed(
            f"Product Brief {brief_id!r} has no readable dispatch-admitted current planning roster"
        )
    observed_task_ids: list[str] = []
    for task in planned_tasks:
        if (
            not isinstance(task, dict)
            or not isinstance(task.get("id"), str)
            or task.get("planning_attempt_id") != planning_attempt_id
            or task.get("dispatch_admitted") is not True
        ):
            return Capture.missed(
                f"Product Brief {brief_id!r} has no readable dispatch-admitted current "
                "planning roster"
            )
        observed_task_ids.append(task["id"])
    if observed_task_ids != released_task_ids:
        return Capture.missed(
            f"Product Brief {brief_id!r} admission released {released_task_ids!r}, not the "
            f"read current planning roster {observed_task_ids!r}"
        )
    if coverage.is_captured and not {row["task_id"] for row in coverage.value} <= set(
        released_task_ids
    ):
        return Capture.missed(
            f"Product Brief {brief_id!r} admission did not release every task coverage names"
        )
    return Capture.captured(
        {
            "brief_id": brief_id,
            "coverage_admitted_at": admitted_at,
            "planning_attempt_id": planning_attempt_id,
            "released_task_ids": released_task_ids,
        }
    )


def _brief_settings_readback_capture(ctx: dict, confirmed: Capture) -> Capture:
    if not confirmed.is_captured:
        return Capture.missed(
            f"settings readback is unread because confirmation is unread: {confirmed.reason}"
        )
    readback = ctx.get("brief_settings_readback")
    if not isinstance(readback, dict):
        return Capture.missed("the product settings/get response was not retained")
    if not isinstance(readback.get("key"), str) or not isinstance(readback.get("scope"), str):
        return Capture.missed("the product settings/get response lacks key or scope")
    if "value" not in readback:
        return Capture.missed("the product settings/get response lacks value")
    expected = confirmed.value["content"].get("initial_settings")
    if not isinstance(expected, list) or not any(
        isinstance(setting, dict)
        and setting.get("key") == readback["key"]
        and setting.get("scope") == readback["scope"]
        and setting.get("value") == readback["value"]
        for setting in expected
    ):
        return Capture.missed(
            "the product settings/get response does not equal an initial setting in the "
            "confirmed Product Brief"
        )
    return Capture.captured(
        {"key": readback["key"], "scope": readback["scope"], "value": readback["value"]}
    )


def _brief_settings_seed_capture(ctx: dict, readback: Capture) -> Capture:
    if not readback.is_captured:
        return Capture.missed(
            f"deploy settings seed is unread because settings readback is unread: {readback.reason}"
        )
    outcomes = ctx.get("brief_settings_seed")
    if not isinstance(outcomes, list):
        return Capture.missed("the deploy Run settings_seed record was not retained")
    expected = readback.value
    matching = [
        outcome
        for outcome in outcomes
        if isinstance(outcome, dict)
        and outcome.get("key") == expected["key"]
        and outcome.get("scope") == expected["scope"]
    ]
    if len(matching) != 1:
        return Capture.missed(
            "the deploy Run settings_seed record does not name exactly one matching confirmed "
            "setting"
        )
    outcome = matching[0]
    if outcome.get("written") is not True or outcome.get("failure") is not None:
        return Capture.missed(
            "the deploy Run settings_seed record does not prove the confirmed setting was written"
        )
    subject_id = outcome.get("subject_id")
    if subject_id is not None and not isinstance(subject_id, int):
        return Capture.missed("the deploy Run settings_seed record has an unreadable subject id")
    return Capture.captured(
        {
            "key": outcome["key"],
            "scope": outcome["scope"],
            "subject_id": subject_id,
            "written": True,
            "failure": None,
        }
    )


def _brief_acceptance_capture(ctx: dict) -> Capture:
    acceptance = ctx.get("brief_acceptance")
    criterion = acceptance.get("criterion") if isinstance(acceptance, dict) else None
    if not isinstance(criterion, dict):
        return Capture.missed(
            "the Architect parsed scheduled acceptance criterion was not retained"
        )
    name = criterion.get("name")
    arguments = criterion.get("arguments")
    observable = criterion.get("observable")
    if name != "multilingual_digest":
        return Capture.missed(
            f"the Architect criterion must name exactly 'multilingual_digest', got {name!r}"
        )
    if arguments != {}:
        return Capture.missed(
            "the Architect criterion for 'multilingual_digest' must have arguments {}, got "
            f"{arguments!r}"
        )
    if not isinstance(observable, str) or not observable:
        return Capture.missed("the Architect criterion has no observable")
    return Capture.captured({"name": name, "arguments": arguments, "observable": observable})


def _brief_job_evidence_capture(ctx: dict, acceptance: Capture) -> Capture:
    if not acceptance.is_captured:
        return Capture.missed(
            f"job evidence is unread because the Architect criterion is unread: {acceptance.reason}"
        )
    evidence = ctx.get("brief_job_evidence")
    if not isinstance(evidence, dict):
        return Capture.missed("the product jobs/evidence response was not retained")
    required_fields = (
        "command_id",
        "name",
        "fired_by_product",
        "fired_by_run",
        "dispatch_status",
        "accepted_at",
    )
    absent = [
        field
        for field in required_fields
        if not isinstance(evidence.get(field), str) or not evidence[field]
    ]
    if absent:
        return Capture.missed(
            "the product jobs/evidence response lacks command provenance fields: "
            + ", ".join(absent)
        )
    if "arguments" not in evidence:
        return Capture.missed("the product jobs/evidence response lacks command arguments")
    if evidence["dispatch_status"] != "dispatched" or not evidence.get("dispatched_at"):
        return Capture.missed(
            "the product jobs/evidence response does not prove that job_fired was dispatched"
        )
    qa_run_id = (ctx.get("qa_run") or {}).get("id")
    if evidence["fired_by_run"] != qa_run_id:
        return Capture.missed(
            f"job evidence was fired by {evidence['fired_by_run']!r}, not QA Run {qa_run_id!r}"
        )
    criterion = acceptance.value
    if evidence["name"] != criterion["name"] or evidence["arguments"] != criterion["arguments"]:
        return Capture.missed(
            "job evidence does not match the Architect criterion: "
            f"name={evidence['name']!r}, arguments={evidence['arguments']!r}"
        )
    expected_command_id = f"qa-{qa_run_id}-{criterion['name']}"
    if evidence["command_id"] != expected_command_id:
        return Capture.missed(
            f"job evidence command id {evidence['command_id']!r} is not {expected_command_id!r}"
        )
    if evidence["fired_by_product"] != ctx.get("project_id"):
        return Capture.missed(
            f"job evidence names product {evidence['fired_by_product']!r}, not "
            f"{ctx.get('project_id')!r}"
        )
    return Capture.captured(
        {
            "command_id": evidence["command_id"],
            "name": evidence["name"],
            "arguments": evidence["arguments"],
            "fired_by_product": evidence["fired_by_product"],
            "fired_by_run": evidence["fired_by_run"],
            "dispatch_status": evidence["dispatch_status"],
            "accepted_at": evidence["accepted_at"],
            "dispatched_at": evidence["dispatched_at"],
        }
    )


def brief_evidence(ctx: dict) -> dict:
    """Evidence only the named Product Brief live scenario is obliged to collect."""
    required = bool(ctx.get("brief_scenario"))
    if not required:
        return {
            "required": False,
            "confirmed": _brief_not_required_capture("confirmation").as_dict(),
            "coverage": _brief_not_required_capture("coverage").as_dict(),
            "admission": _brief_not_required_capture("admission").as_dict(),
            "acceptance": _brief_not_required_capture("acceptance criterion").as_dict(),
            "settings_readback": _brief_not_required_capture("settings readback").as_dict(),
            "settings_seed": _brief_not_required_capture("deploy settings seed").as_dict(),
            "job_evidence": _brief_not_required_capture("job evidence").as_dict(),
        }
    confirmed = _brief_confirmed_capture(ctx)
    coverage = _brief_coverage_capture(ctx, confirmed)
    admission = _brief_admission_capture(ctx, confirmed, coverage)
    acceptance = _brief_acceptance_capture(ctx)
    settings = _brief_settings_readback_capture(ctx, confirmed)
    settings_seed = _brief_settings_seed_capture(ctx, settings)
    job = _brief_job_evidence_capture(ctx, acceptance)
    return {
        "required": True,
        "confirmed": confirmed.as_dict(),
        "coverage": coverage.as_dict(),
        "admission": admission.as_dict(),
        "acceptance": acceptance.as_dict(),
        "settings_readback": settings.as_dict(),
        "settings_seed": settings_seed.as_dict(),
        "job_evidence": job.as_dict(),
    }


def verdict(
    ctx: dict,
    terminal_state: TerminalState,
    failure_kind: FailureKind,
    reason: Capture,
    *,
    worker_executed: dict,
    qa_executed: dict,
    brief: dict,
) -> dict:
    """Red or green, and every reason for red carrying its control-plane reason.

    On a paid run, executor evidence that came back `missed` is a finding, not a
    silence: a combination that spent a subscription and cannot show which agent
    ran is red, and the reason it is red is stated together with the control
    plane's account of the stage that stopped it. The free deterministic route
    starts no such container by design, so its verdict is what it always was —
    the terminal state and nothing else.
    """
    paid = is_paid_run(ctx)
    reasons: list[dict] = []
    if terminal_state is not TerminalState.COMPLETED:
        reasons.append(
            {
                "code": VerdictReason.RUN_FAILED.value,
                "detail": (
                    f"the combination stopped at {terminal_state.value} ({failure_kind.value})"
                ),
                "control_plane_reason": reason.as_dict(),
            }
        )
    if paid:
        if worker_executed["status"] == CaptureStatus.MISSED.value:
            reasons.append(
                {
                    "code": VerdictReason.WORKER_EXECUTED_MISSED.value,
                    "detail": (
                        "this paid run cannot show which developer agent executed: "
                        f"{worker_executed['reason']}"
                    ),
                    "control_plane_reason": reason.as_dict(),
                }
            )
        if ctx.get("qa_requires_executor") and qa_executed["status"] == CaptureStatus.MISSED.value:
            reasons.append(
                {
                    "code": VerdictReason.QA_EXECUTED_MISSED.value,
                    "detail": (
                        "this paid run asked for an LLM QA executor and cannot show which "
                        f"one executed: {qa_executed['reason']}"
                    ),
                    "control_plane_reason": reason.as_dict(),
                }
            )
    if brief["required"]:
        for name in (
            "confirmed",
            "coverage",
            "admission",
            "acceptance",
            "settings_readback",
            "settings_seed",
            "job_evidence",
        ):
            capture = brief[name]
            if capture["status"] == CaptureStatus.MISSED.value:
                reasons.append(
                    {
                        "code": VerdictReason.BRIEF_EVIDENCE_MISSED.value,
                        "detail": (
                            f"the required Product Brief {name} evidence is missed: "
                            f"{capture['reason']}"
                        ),
                        "control_plane_reason": reason.as_dict(),
                    }
                )
    return {
        "paid": paid,
        "status": (Verdict.RED if reasons else Verdict.GREEN).value,
        "reasons": reasons,
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
    reason = control_plane_reason(ctx, terminal_state, failure_kind)
    worker_executed = collector.executed_worker_agent().as_dict()
    qa = qa_cell(ctx)
    brief = brief_evidence(ctx)
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "kind": EVIDENCE_KIND,
        "generated_at": generated_at.isoformat(),
        "combination": {
            "label": combination_label(ctx),
            "worker_requested": ctx.get("agent_type"),
            "worker_executed": worker_executed,
            "qa_requested": ctx.get("qa_agent_type_requested"),
            "qa_executed": qa["executor_executed"],
        },
        "failure": failure_summary(ctx, terminal_state, failure_kind, reason),
        "verdict": verdict(
            ctx,
            terminal_state,
            failure_kind,
            reason,
            worker_executed=worker_executed,
            qa_executed=qa["executor_executed"],
            brief=brief,
        ),
        "engineering": engineering_evidence(ctx),
        "deployment": deployment_evidence(ctx),
        "debug_dumps": sorted(ctx.get("debug_dumps") or []),
        "project": {
            "id": ctx.get("project_id"),
            "name": ctx.get("project_name"),
            "story_id": ctx.get("story_id"),
            "task_id": ctx.get("task_id"),
        },
        "tasks": ctx.get("task_diagnostics", {}),
        "discovery": {
            "run_id": collector.run_id,
            "docker_filters": [f"label={label}" for label in _run_label_filters(collector.run_id)],
            "removal_records": removed_worker_evidence_key(collector.run_id),
            "note": (
                "Workers are found by the run label their container was stamped with at "
                "creation, so one that exited — and whose Redis metadata is already "
                "deleted — is still listed and still readable. A container that was "
                "removed is not listed at all, so worker-manager reads its exit code and "
                "log tail before removing it and files them under the run: those arrive "
                "here as discovered_by=delete_capture. A worker in neither source is "
                "recorded as a missed capture saying so."
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
        "qa": qa,
        "brief": brief,
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
    # Resolved here as well as in build_artifact: the live harness calls this with
    # no root, and `None / "docs"` raised inside the fixture's `finally`, failing
    # every combination at teardown and writing no artifact at all.
    artifact_root = root if root is not None else orchestrator_root()
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
    artifact = build_artifact(ctx, root=artifact_root)
    return write_artifact(artifact, evidence_output_directory(root))

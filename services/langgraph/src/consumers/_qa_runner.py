"""Run deterministic QA checks, then the one assigned subscription QA executor."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
import re
from typing import Protocol

import asyncssh
import httpx
import structlog

from shared.contracts.acceptance import (
    HealthCriterion,
    ScheduledBehaviourCriterion,
    parse_scheduled_behaviours,
)
from shared.contracts.dto.product_brief import InitialSetting
from shared.contracts.dto.run_result import (
    QABlocker,
    QABlockerCategory,
    QATelegramProbeEvidence,
)
from shared.contracts.queues.worker import WorkerOwnership
from shared.contracts.vocab import AgentType
from shared.qa_identity import QAIdentityRejection
from shared.telegram_access_probe import (
    build_access_probe_script,
    classify_access_probe,
    run_probe_script,
)

from ..agents.qa.acceptance import prepare_central_qa_criteria
from ..agents.qa.capability_service import QACapabilityService
from ..agents.qa.packages import (
    ACTIVE_PACKAGE_CONTRACT,
    BACKEND_MANIFEST,
    CONTRACT_READ_LIMIT,
    GENERATED_JOB_REGISTRY,
    ActivePackage,
    PackageActivation,
    PackageContractUnreadable,
    active_package_facts,
    behaviour_check,
    connection_check,
    parse_active_packages,
    parse_job_owners,
    parse_listed_packages,
)
from ..agents.qa.tools import QAJobsCapability, build_qa_callables
from ..clients.qa_worker import QAExecutorRun, QAExecutorUnavailable, run_qa_executor
from ..prompts.qa import build_qa_instructions, build_qa_prompt
from ._qa_target import (
    CONTAINER_PROBE_ATTEMPTS,
    CONTAINER_PROBE_RETRY_DELAY,
    READ_NOT_A_FILE,
    QACapabilityError,
    QAContainerRuntimeError,
    QAGrantError,
    QAGrantJournal,
    QAGrantOutcome,
    QAIdentityAbsentError,
    QAIdentityUnreadableError,
    QATarget,
    QATargetError,
    new_grant_marker,
    qa_target_grant,
)
from ._qa_workspace import QAWorkspace, qa_workspace

logger = structlog.get_logger(__name__)

QA_TIMEOUT = 1200  # 20 minutes
# Retry only transient subscription-executor failures.
QA_EXECUTOR_ATTEMPTS = 2
HEALTH_CHECK_TIMEOUT = 30
HEALTH_CHECK_ATTEMPTS = 5
HEALTH_CHECK_RETRY_DELAY = 5
ACCESS_PROBE_TIMEOUT = 60
CONTAINER_HEALTHY = "healthy"
#: The product's own terminal dispatch state for a command whose event was
#: emitted. Anything else — `undelivered` — is the product saying the event
#: never left, which no behaviour can have run from.
DISPATCHED = "dispatched"
_WRITE_METHODS = "POST|PUT|PATCH|DELETE"


@dataclass(frozen=True)
class QARuntimeConfig:
    """Assigned executor and management-host capability/Telegram configuration."""

    executor_agent_type: AgentType
    capability_host: str
    telethon_env: dict[str, str] | None = None


# One header per retained attempt, so a body carrying two of them is readable as
# two attempts rather than as one confusing transcript. It is presentation, and
# presentation only ever goes *around* output an executor produced: a header with
# nothing under it would be this code's own text published as though an agent had
# written it.
EXECUTOR_ATTEMPT_HEADER = "== QA executor attempt {attempt} of {attempts} =="


@dataclass(frozen=True)
class QAExecutorAttempts:
    """What every executor attempt of one QA run said, in the order they ran.

    An attempt that never started a container contributes nothing — there is no
    transcript to keep — and an attempt that ran contributes what it said, the
    empty string included, because "it ran and was silent" is something this
    process observed.

    `evidence` keeps those three answers apart, and they never merge:

    * a non-empty string — the attempts that produced output, each under its own
      header. An attempt that ran and said nothing is not given a header: there
      is nothing for the header to introduce, and assembled text is never
      content;
    * ``""`` — at least one attempt ran and no attempt said anything. The runner
      watched that happen, so the silence is knowledge and is carried as such;
    * ``None`` — no attempt ever started a container, so this record holds
      nothing and claims nothing about any executor.
    """

    attempts: int
    said: tuple[tuple[int, str], ...] = ()

    def with_attempt(self, attempt: int, transcript: str | None) -> QAExecutorAttempts:
        if transcript is None:
            return self
        return QAExecutorAttempts(self.attempts, (*self.said, (attempt, transcript)))

    @property
    def evidence(self) -> str | None:
        if not self.said:
            return None
        spoke = [(attempt, text) for attempt, text in self.said if text]
        if not spoke:
            return ""
        return "\n".join(
            f"{EXECUTOR_ATTEMPT_HEADER.format(attempt=attempt, attempts=self.attempts)}\n{text}"
            for attempt, text in spoke
        )


class QAInfrastructureFailure(Exception):
    """Typed infrastructure failure that must not become a product verdict.

    `executor_transcript` carries what the executor attempts of this run said
    when one or more ran and the run still ended as infrastructure — a container
    that started, produced output and never reached the capability endpoint. It
    is ``None`` when no attempt of this run started a container, so there was
    never anything to carry.
    """

    def __init__(
        self,
        *,
        summary: str,
        blocker: QABlocker,
        executor_transcript: str | None = None,
    ) -> None:
        super().__init__(blocker.received)
        self.summary = summary
        self.blocker = blocker
        self.executor_transcript = executor_transcript


@dataclass
class QAResult:
    """Structured result from a QA run."""

    passed: bool
    checks: list[dict] = field(default_factory=list)
    summary: str = ""
    raw: str = ""
    report: str = ""
    blocker: QABlocker | None = None
    state_changes: list[dict] = field(default_factory=list)
    telegram_probe_evidence: list[QATelegramProbeEvidence] = field(default_factory=list)
    # The executor's own account of the run, scanned with runner-owned evidence
    # for forbidden writes and carried across the Run boundary
    # (`QARunResult.executor_transcript`) because it exists nowhere else once the
    # stand is gone. ``None`` is "no executor ran at all" — deterministic health
    # checks, or a container-state failure that never started one — and an empty
    # string is an executor that ran and said nothing. A red run's artifact
    # reports those as different findings, so they are kept apart here.
    executor_evidence: str | None = None


def _unknown_result_blocker(*, attempted: str, sent: str, received: str) -> QABlocker:
    """Build a fail-closed blocker when QA has no trustworthy product judgement."""
    return QABlocker(
        category=QABlockerCategory.UNKNOWN,
        attempted=attempted,
        sent=sent,
        received=received,
    )


def _forbidden_application_write(trace: str, deployed_url: str) -> str | None:
    """Return the first application write found in runner-visible QA evidence."""
    escaped_url = re.escape(deployed_url.rstrip("/"))
    patterns = (
        rf"(?i)\b({_WRITE_METHODS})\s+({escaped_url}[^\s'\"]*)",
        rf"(?i)(?:-X|--request)\s+({_WRITE_METHODS})\b[^\n]*?({escaped_url}[^\s'\"]*)",
        rf"(?i)\bcurl\b(?![^\n]*?\s(?:-G|--get)\b)[^\n]*?\s(?:-d|--data(?:-raw|-binary|-ascii)?)(?:=|\s)[^\n]*?({escaped_url}[^\s'\"]*)",
        rf"(?i)\b(?:requests|httpx)\.({_WRITE_METHODS.lower()})\s*\(\s*['\"]({escaped_url}[^'\"]*)",
    )
    for pattern in patterns:
        match = re.search(pattern, trace)
        if match:
            if len(match.groups()) == 1:
                return f"POST {match.group(1)}"
            return f"{match.group(1).upper()} {match.group(2)}"
    return None


def _block_forbidden_application_write(qa_result: QAResult, write: str) -> QAResult:
    """Fail closed when QA evidence shows a direct application API write."""
    qa_result.passed = False
    qa_result.summary = "QA attempted a forbidden application API write"
    qa_result.blocker = QABlocker(
        category=QABlockerCategory.UNKNOWN,
        attempted="verify QA used only read-only application API requests",
        sent=write,
        received="application state may have changed; no generic rollback is available",
    )
    qa_result.state_changes = [
        {
            "resource": write,
            "operation": "modified",
            "cleanup": {
                "attempted": False,
                "succeeded": False,
                "detail": (
                    "forbidden direct application write detected; residual state is unverified"
                ),
            },
        }
    ]
    return qa_result


def _invalid_qa_payload(raw: str, reason: str) -> QAResult:
    """Fail closed when the agent's result cannot safely drive QA routing."""
    return QAResult(
        passed=False,
        summary=f"QA output has an invalid result shape: {reason}",
        raw=raw,
        blocker=_unknown_result_blocker(
            attempted="validate QA agent result",
            sent="QA agent final message",
            received=raw[:2000],
        ),
    )


def _validate_qa_payload(data: dict, raw: str) -> QAResult | None:
    """Fail closed unless every routing-relevant response field is valid."""
    required_fields = {"pass", "checks", "summary"}
    # Cleanup evidence is runner-owned, not trusted agent output.
    allowed_fields = required_fields | {"state_changes"}
    if not required_fields <= set(data) or not set(data) <= allowed_fields:
        return _invalid_qa_payload(
            raw,
            "expected exactly pass, checks, and summary fields",
        )

    if not isinstance(data["pass"], bool):
        return _invalid_qa_payload(raw, "pass must be a boolean")
    if not isinstance(data["summary"], str):
        return _invalid_qa_payload(raw, "summary must be a string")
    if not isinstance(data["checks"], list):
        return _invalid_qa_payload(raw, "checks must be a list")

    expected_check_fields = {"name", "pass", "detail"}
    for index, check in enumerate(data["checks"]):
        if not isinstance(check, dict) or set(check) != expected_check_fields:
            return _invalid_qa_payload(
                raw,
                f"check {index} must contain exactly name, pass, and detail fields",
            )
        if not isinstance(check["name"], str) or not check["name"].strip():
            return _invalid_qa_payload(raw, f"check {index} name must be a non-empty string")
        if not isinstance(check["pass"], bool):
            return _invalid_qa_payload(raw, f"check {index} pass must be a boolean")
        if not isinstance(check["detail"], str) or not check["detail"].strip():
            return _invalid_qa_payload(raw, f"check {index} detail must be a non-empty string")

    return None


def parse_qa_result(raw: str) -> QAResult:
    """Parse raw, fenced, or result-wrapped QA JSON into a QAResult."""
    if not raw or not raw.strip():
        return QAResult(
            passed=False,
            summary="QA produced no output",
            raw=raw,
            blocker=_unknown_result_blocker(
                attempted="parse QA agent result",
                sent="QA agent final message",
                received="empty output",
            ),
        )

    json_str = raw.strip()

    try:
        wrapper = json.loads(json_str)
        if isinstance(wrapper, dict) and wrapper.get("type") == "result":
            json_str = wrapper.get("result", "")
    except json.JSONDecodeError:
        pass  # Not a wrapper, continue with raw

    code_block_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", json_str, re.DOTALL)
    if code_block_match:
        json_str = code_block_match.group(1).strip()

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        return QAResult(
            passed=False,
            summary=f"Failed to parse QA output as JSON: {raw[:200]}",
            raw=raw,
            blocker=_unknown_result_blocker(
                attempted="parse QA agent result",
                sent="QA agent final message",
                received=raw[:2000],
            ),
        )

    if not isinstance(data, dict):
        return QAResult(
            passed=False,
            summary="QA output is not a result object",
            raw=raw,
            blocker=_unknown_result_blocker(
                attempted="validate QA agent result",
                sent="QA agent final message",
                received=raw[:2000],
            ),
        )

    invalid_result = _validate_qa_payload(data, raw)
    if invalid_result:
        return invalid_result

    return QAResult(
        passed=data["pass"],
        checks=data["checks"],
        summary=data["summary"],
        raw=raw,
    )


async def run_health_checks(
    *,
    deployed_url: str,
    checks: list[HealthCriterion],
) -> QAResult:
    """Run GET criteria against the deployed URL. No SSH, no LLM.

    Each check is retried while the service is still coming up; a check that
    never answers with its expected status fails the run.
    """
    results = []
    transport_failures: list[tuple[str, httpx.TransportError]] = []
    # "returns 200" means the path itself answers 200. Following redirects would
    # report the destination's status instead, so a criterion naming a redirect
    # could never pass and one naming 200 would pass on a redirected path.
    async with httpx.AsyncClient(timeout=HEALTH_CHECK_TIMEOUT, follow_redirects=False) as client:
        for check in checks:
            result, transport_error = await _run_health_check(client, deployed_url, check)
            results.append(result)
            if transport_error:
                transport_failures.append((check.path, transport_error))

    failed = [c for c in results if not c["pass"]]
    passed = not failed
    summary = (
        f"{len(results)} GET check(s) passed against {deployed_url}"
        if passed
        else f"{len(failed)}/{len(results)} GET check(s) failed against {deployed_url}"
    )
    logger.info("qa_health_checks_done", deployed_url=deployed_url, passed=passed)
    blocker = None
    if transport_failures:
        path, error = transport_failures[0]
        blocker = QABlocker(
            category=QABlockerCategory.DEPLOYED_URL_UNREACHABLE,
            attempted="run health check against deployed URL",
            sent=f"GET {deployed_url.rstrip('/')}{path}",
            received=f"transport error: {error}",
        )
    return QAResult(
        passed=passed,
        checks=results,
        summary=summary,
        report="\n".join(f"- {c['name']}: {c['detail']}" for c in results),
        blocker=blocker,
    )


async def check_deployed_url_reachable(deployed_url: str) -> QABlocker | None:
    """Check that the deployment can be contacted before starting an agent.

    A response, including a non-2xx response, proves the URL is reachable. The
    acceptance criteria decide whether that response is a product failure.
    """
    try:
        async with httpx.AsyncClient(
            timeout=HEALTH_CHECK_TIMEOUT, follow_redirects=False
        ) as client:
            await client.get(deployed_url)
    except httpx.HTTPError as exc:
        return QABlocker(
            category=QABlockerCategory.DEPLOYED_URL_UNREACHABLE,
            attempted="GET deployed URL before starting QA agent",
            sent=f"GET {deployed_url}",
            received=f"transport error: {exc}",
        )
    return None


async def _run_health_check(
    client: httpx.AsyncClient,
    deployed_url: str,
    check: HealthCriterion,
) -> tuple[dict, httpx.TransportError | None]:
    """GET one path, retrying until it answers as expected or attempts run out."""
    name = f"GET {check.path} returns {check.expected_status}"
    detail = "no response"
    transport_error = None
    for attempt in range(HEALTH_CHECK_ATTEMPTS):
        if attempt:
            await asyncio.sleep(HEALTH_CHECK_RETRY_DELAY)
        try:
            response = await client.get(f"{deployed_url.rstrip('/')}{check.path}")
        except httpx.TransportError as e:
            detail = f"request failed: {e}"
            transport_error = e
            continue
        if response.status_code == check.expected_status:
            return {"name": name, "pass": True, "detail": f"got {response.status_code}"}, None
        detail = f"got {response.status_code}, expected {check.expected_status}"
        transport_error = None
    logger.warning("qa_health_check_failed", path=check.path, detail=detail)
    return {"name": name, "pass": False, "detail": detail}, transport_error


@dataclass(frozen=True)
class _ContainerState:
    """One container of this deployment, as docker reported it."""

    name: str
    ok: bool
    detail: str

    def as_check(self) -> dict:
        return {"name": f"container {self.name} is running", "pass": self.ok, "detail": self.detail}


class _ContainerStateUnreadable(Exception):
    """Docker did not answer with a container state. Says nothing about the product."""


def read_container_state(name: str, payload: str) -> _ContainerState:
    """Decide one container's state from `docker inspect --format {{json .State}}`.

    The rules are the ones a human would apply to that output and nothing more:
    a container that is restarting is in a restart loop, one that is not running
    is down, and one whose image declares a health check has to be `healthy`.
    Containers without a health check have no `Health` key at all — that is
    docker's schema, not a missing value, which is why it is the only field read
    conditionally.

    Raises:
        _ContainerStateUnreadable: the payload is not a docker container state.
    """
    try:
        state = json.loads(payload)
        status = state["Status"]
        running = state["Running"]
        restarting = state["Restarting"]
        exit_code = state["ExitCode"]
        health = state["Health"]["Status"] if "Health" in state else ""
    except (ValueError, TypeError, KeyError) as exc:
        raise _ContainerStateUnreadable(
            f"docker inspect of {name} did not answer with a container state: {payload[:300]!r}"
        ) from exc
    if restarting:
        return _ContainerState(name, False, f"restarting (last exit code {exit_code})")
    if not running:
        return _ContainerState(name, False, f"{status} (exit code {exit_code})")
    if health and health != CONTAINER_HEALTHY:
        return _ContainerState(name, False, f"running, health {health}")
    return _ContainerState(name, True, f"running{f', health {health}' if health else ''}")


async def _read_container_states(
    session, containers: list[str]
) -> tuple[list[_ContainerState], str]:
    """Inspect every container of this deployment once. Returns states and a failure."""
    states: list[_ContainerState] = []
    for name in containers:
        try:
            remote = await session.container_inspect(name)
        except (OSError, asyncssh.Error) as exc:
            return states, f"the target did not answer docker inspect of {name}: {exc}"
        if remote.exit_status != 0:
            detail = (remote.stderr or remote.stdout or "no output").strip()[:300]
            return states, f"docker inspect of {name} exited {remote.exit_status}: {detail}"
        try:
            states.append(read_container_state(name, remote.stdout))
        except _ContainerStateUnreadable as exc:
            return states, str(exc)
    return states, ""


def container_runtime_unavailable(*, sent: str, received: str) -> QAInfrastructureFailure:
    """The one place that says what an unanswering container runtime is.

    Two calls can meet that condition: the `docker ps` that builds the run's
    capability set, and the `docker inspect` of each container this probe reads.
    They are the same fact about the target, so they are classified here and only
    here — a QA-infrastructure outcome with bounded retries already spent and an
    administrator alert to follow, never a verdict about the product and never
    "the server could not be reached", which means something else entirely.
    """
    return QAInfrastructureFailure(
        summary="QA could not be performed: the target's container runtime did not answer",
        blocker=QABlocker(
            category=QABlockerCategory.QA_PROBE_UNAVAILABLE,
            attempted="read the state of this deployment's containers before starting QA",
            sent=sent,
            received=received,
        ),
    )


async def run_container_state_checks(session) -> QAResult:
    """Read the state of this deployment's containers. No LLM, no agent.

    This is a fact about the deployment, so it is established the same way the
    GET criteria are: by asking, here, before any executor exists. It uses the
    run's own session and the same `container_inspect` the exploratory agent
    would have called — the point is who asks, not a new way of asking.

    A container that is down, looping or unhealthy is a failed QA check, which
    is a product defect and is routed as one. Docker not answering is not: that
    is infrastructure, and it is raised rather than returned.

    Raises:
        QAInfrastructureFailure: docker did not answer, or this deployment has
            no containers at all — in both cases nothing about the product was
            established, and the platform is what has to be repaired.
    """
    containers = sorted(session.capabilities.containers)
    target = session.target
    if not containers:
        raise QAInfrastructureFailure(
            summary="QA could not be performed: the deployment has no containers to inspect",
            blocker=QABlocker(
                category=QABlockerCategory.QA_PROBE_UNAVAILABLE,
                attempted="read the state of this deployment's containers before starting QA",
                sent=f"docker ps of compose project {target.project_name} on {target.server_ip}",
                received=(
                    "docker reports no container for this deployment, so its state cannot be "
                    "read and nothing about the product can be concluded from it"
                ),
            ),
        )

    states: list[_ContainerState] = []
    failure = ""
    for attempt in range(CONTAINER_PROBE_ATTEMPTS):
        if attempt:
            await asyncio.sleep(CONTAINER_PROBE_RETRY_DELAY)
        states, failure = await _read_container_states(session, containers)
        if not failure and all(state.ok for state in states):
            break
    if failure:
        logger.error("qa_container_probe_unavailable", server_ip=target.server_ip, detail=failure)
        raise container_runtime_unavailable(
            sent=f"docker inspect of {', '.join(containers)} on {target.server_ip}",
            received=failure,
        )

    checks = [state.as_check() for state in states]
    failed = [state for state in states if not state.ok]
    summary = (
        f"{len(states)} container(s) of {target.project_name} are running"
        if not failed
        else f"{len(failed)}/{len(states)} container(s) of {target.project_name} are not running"
    )
    logger.info(
        "qa_container_state_probed",
        server_ip=target.server_ip,
        project_name=target.project_name,
        passed=not failed,
        failed=[state.name for state in failed],
    )
    return QAResult(
        passed=not failed,
        checks=checks,
        summary=summary,
        report="\n".join(f"- {check['name']}: {check['detail']}" for check in checks),
    )


def container_state_fact(probe: QAResult) -> str:
    """What the container probe established, as a line the executor is told.

    The exploratory agent is not asked to find this out again: it already
    happened, deterministically, against the same deployment moments earlier.
    """
    containers = "; ".join(f"{check['name']} — {check['detail']}" for check in probe.checks)
    return f"- Container state, read from the target with docker inspect: {containers}."


@dataclass(frozen=True)
class PackageActivationOutcome:
    """What the deployment said about its own kit packages.

    Exactly one of the two is set, and the third case is both unset: a product
    that carries no package contract at all. That product's run is the run it
    always was — no package artifact was read, so nothing about packages is
    stated, and a deployment cannot be talked into a package by silence.
    """

    activation: PackageActivation | None = None
    failure: QAResult | None = None


class _ContractUnread(Exception):
    """One generated artifact of the deployment could not be read."""


async def _read_generated_contract(session, path: str) -> str | None:
    """Read one generated artifact of the deployment, or say it is not there.

    ``None`` is "this deployment has no such file", which is an answer about
    the product. Anything else the target says about the read is not an answer
    about the product and is raised, because a read that failed must never
    become the sentence "this product has no packages".
    """
    try:
        remote = await session.read_file(path, max_bytes=CONTRACT_READ_LIMIT)
    except QATargetError as exc:
        # The contained read resolves on the target, so a path whose parent
        # directory does not exist is reported the same way: not there.
        if "does not exist on the target" in str(exc):
            return None
        raise _ContractUnread(f"{path} could not be read from the deployment: {exc}") from exc
    if remote.exit_status == READ_NOT_A_FILE:
        return None
    if remote.exit_status != 0:
        detail = (remote.stderr or remote.stdout or "no output").strip()[:300]
        raise _ContractUnread(f"{path} could not be read from the deployment: {detail}")
    return remote.stdout


def _package_contract_failure(detail: str) -> QAResult:
    """A package contract that could not be established fails the run.

    It is a product verdict rather than an infrastructure blocker: these are
    the product's own generated artifacts, on its own deployment, and a
    product whose package contract cannot be read is a product whose runtime
    would refuse to boot on it. Reporting it as "no packages" is the vacuous
    pass this path exists to prevent.
    """
    check = {
        "name": "the deployed product's package contract is readable",
        "pass": False,
        "detail": detail,
    }
    return QAResult(
        passed=False,
        checks=[check],
        summary="QA could not establish which kit packages the deployed product carries",
        report=f"- {check['name']}: {detail}",
    )


async def run_package_activation_checks(session) -> PackageActivationOutcome:
    """Establish, from the deployment itself, which kit packages it is running.

    Three artifacts of the product answer it, and they are cross-checked
    against each other rather than trusted one at a time: the backend
    manifest's allowlist, the generated package contract that records the set
    generation resolved with its manifest digests, and the generated job
    registry that attributes each fireable job to the service or package that
    declared it. A product that booted is a product whose generated contract
    still matches its manifest and installed wheels — the runtime refuses a
    stale one — so this is the package's connection check, read from the
    running product instead of re-run against a copy of it.

    A product with no packages, and a product from a template that predates
    them, both come back with nothing set: their run is unchanged. Every other
    disagreement fails the run before an executor starts.
    """
    try:
        manifest = await _read_generated_contract(session, BACKEND_MANIFEST)
        listed = parse_listed_packages(manifest) if manifest is not None else None
        contract = await _read_generated_contract(session, ACTIVE_PACKAGE_CONTRACT)
        packages = parse_active_packages(contract) if contract is not None else None
    except (_ContractUnread, PackageContractUnreadable) as exc:
        return PackageActivationOutcome(failure=_package_contract_failure(str(exc)))

    if listed is None and packages is None:
        # Nothing in this deployment claims a package, in either place a
        # package is claimed. There is no package check to make.
        return PackageActivationOutcome()
    if listed is None or packages is None:
        missing = BACKEND_MANIFEST if listed is None else ACTIVE_PACKAGE_CONTRACT
        return PackageActivationOutcome(
            failure=_package_contract_failure(
                f"the deployment declares packages in one place and not the other: "
                f"{missing} is not there, while the other names "
                f"{', '.join(listed or [package.name for package in packages or ()]) or 'none'}"
            )
        )
    if sorted(package.name for package in packages) != sorted(listed):
        return PackageActivationOutcome(
            failure=_package_contract_failure(
                f"the backend manifest lists {', '.join(listed) or 'no package'} while "
                f"{ACTIVE_PACKAGE_CONTRACT} records "
                f"{', '.join(package.name for package in packages) or 'no package'}; the "
                "deployed product's generated contract and its allowlist disagree"
            )
        )
    if not packages:
        return PackageActivationOutcome()

    try:
        registry = await _read_generated_contract(session, GENERATED_JOB_REGISTRY)
        if registry is None:
            raise _ContractUnread(
                f"{GENERATED_JOB_REGISTRY} is not there, so the deployed product "
                "attributes no job to the packages it is running"
            )
        jobs = parse_job_owners(registry)
    except (_ContractUnread, PackageContractUnreadable) as exc:
        return PackageActivationOutcome(failure=_package_contract_failure(str(exc)))

    activation = PackageActivation(packages=packages, listed=tuple(listed), jobs=jobs)
    logger.info(
        "qa_active_packages_probed",
        packages=[package.stated for package in activation.packages],
        package_jobs=sorted(activation.package_jobs),
    )
    return PackageActivationOutcome(activation=activation)


@dataclass(frozen=True)
class PackageAcceptance:
    """The results an active package makes one QA run owe.

    One the runner performs itself, before an executor exists: the package is
    active in the booted product. The other is the package's declared
    behaviour, and it is not one the runner can perform — only the product's
    own output answers the observable a criterion states — so what is kept here
    is which behaviours this run may fire, and the row is decided afterwards
    from the runner's own record of what the run fired and read.

    There is deliberately no prefixed-route row. Package protocol v1 keeps a
    package's `http.prefix` in the installed `package.yaml` inside the wheel,
    and the generated contract records only name, version and manifest digest,
    so a deployed product never tells QA where its package is mounted. A check
    QA cannot address is not made into one by inferring a prefix from the
    package's name: that would fail a healthy package over a fact the product
    never published. The run records that it could not determine the route and
    claims nothing about it.
    """

    activation: PackageActivation
    checks: tuple[dict, ...]
    #: Per package, the behaviours this run's criteria declared and the
    #: deployed product attributes to that package, with the observable each
    #: criterion states.
    declared: Mapping[str, tuple[ScheduledBehaviourCriterion, ...]]
    #: Per package, every behaviour the deployed product attributes to it.
    owned: Mapping[str, tuple[str, ...]]
    #: Whether this deployment offers a fire at all. A deployment holding no
    #: jobs capability for the QA runtime cannot be fired, and that is a
    #: different reason for the same failed row.
    fireable: bool = True


def run_package_acceptance_checks(
    activation: PackageActivation,
    *,
    criteria_behaviours: Sequence[ScheduledBehaviourCriterion] = (),
    fireable: bool = True,
) -> PackageAcceptance:
    """Establish what an active package makes this run owe.

    The connection check is read off the booted product's own generated
    contract, which is performing it: the package's `startup` raises on
    failure, the runtime refuses a contract that no longer matches the manifest
    and the installed wheels, and the deployment is up.

    The kit's remaining acceptance steps — install the wheel, resolve the entry
    point, start the application, validate the manifest, run the import lint —
    are build-time proofs and are deliberately not here. They happen where the
    product is built, and asking a read-only QA run to perform them would prove
    nothing the build has not already proven.
    """
    checks: list[dict] = []
    declared: dict[str, tuple[ScheduledBehaviourCriterion, ...]] = {}
    owned: dict[str, tuple[str, ...]] = {}
    for package in activation.packages:
        checks.append(connection_check(package))
        package_jobs = [
            job for job, owner in activation.package_jobs.items() if owner == package.name
        ]
        owned[package.name] = tuple(sorted(package_jobs))
        declared[package.name] = tuple(
            criterion for criterion in criteria_behaviours if criterion.name in package_jobs
        )
    logger.info(
        "qa_package_acceptance_owed",
        packages=activation.names,
        owned={name: list(names) for name, names in owned.items()},
        declared={
            name: [criterion.name for criterion in criteria] for name, criteria in declared.items()
        },
        route_undetermined=activation.names,
    )
    return PackageAcceptance(
        activation=activation,
        checks=tuple(checks),
        declared=declared,
        owned=owned,
        fireable=fireable,
    )


def _judged_check(behaviour: str, submitted: Sequence[dict]) -> str:
    """The run's own passing check for this behaviour, if it reported one."""
    wanted = behaviour.casefold()
    for check in submitted:
        if not isinstance(check, dict) or not check.get("pass"):
            continue
        stated = f"{check.get('name', '')} {check.get('detail', '')}".casefold()
        if wanted in stated:
            return str(check.get("name", ""))
    return ""


def _behaviour_row(
    package: ActivePackage,
    criterion: ScheduledBehaviourCriterion,
    acceptance: PackageAcceptance,
    workspace: QAWorkspace,
    submitted,
) -> dict:
    """What this run may say about one declared behaviour of one package.

    Three things have to hold for this exact name, and each missing one is its
    own reason: the deployment offered a fire and the product accepted one, the
    run read the product's own recorded command for that same name with
    `job_evidence`, and the run reported a passing check naming the behaviour.

    The evidence read is the anchor, and it is the one this bullet's contract
    names: `POST /jobs/evidence` answers with a command only within the product
    that fired it, so it is bound to this run, this deployment and this
    behaviour in a way no other read is. What it establishes is that the named
    evidence was read — not that the criterion's prose observable was met. That
    judgement is the executor's, and this row requires the executor to have
    made it in a check that names the behaviour.
    """
    name = criterion.name
    if not acceptance.fireable:
        return behaviour_check(
            package,
            behaviour=name,
            reason=(
                f"this run's criteria declare {name} for package {package.name}, and this "
                "deployment holds no jobs capability for the QA runtime, so no fire could be "
                "made and the behaviour was never exercised"
            ),
        )
    fired = workspace.fired_behaviours.get(name)
    if fired is None:
        return behaviour_check(
            package,
            behaviour=name,
            reason=(
                f"this run's criteria declare {name} for package {package.name}, and the "
                "deployed product accepted no fire of it in this run, so the behaviour was "
                "never exercised"
            ),
        )
    evidence = workspace.behaviour_evidence.get(name)
    if evidence is None or evidence.position < fired:
        return behaviour_check(
            package,
            behaviour=name,
            reason=(
                f"the deployed product accepted this run's fire of {name}, and the run never "
                f"read the product's own recorded command for it (job_evidence {name}) "
                "afterwards. A fire is answered with a dispatch record, which is not evidence "
                "the behaviour ran, so this result would rest on the acknowledgement alone. "
                f"The observable the criterion states: {criterion.observable}"
            ),
        )
    if evidence.dispatch_status != DISPATCHED:
        return behaviour_check(
            package,
            behaviour=name,
            reason=(
                f"the product's own recorded command for {name} says dispatch_status="
                f"{evidence.dispatch_status}: the event was never emitted, so nothing can have "
                f"consumed it and the observable the criterion states was not produced: "
                f"{criterion.observable}"
            ),
        )
    judged = _judged_check(name, submitted)
    if not judged:
        return behaviour_check(
            package,
            behaviour=name,
            reason=(
                f"this run fired {name} and read the product's recorded evidence for it, but "
                f"its result carries no passing check naming {name}, so nothing judged the "
                f"observable the criterion states: {criterion.observable}"
            ),
        )
    return behaviour_check(package, behaviour=name, observable=criterion.observable, judged=judged)


def apply_package_acceptance(
    qa_result: QAResult, acceptance: PackageAcceptance | None, workspace: QAWorkspace
) -> QAResult:
    """Require the package results, whatever the executor submitted.

    Every declared behaviour of every active package gets its own row: two
    behaviours are two results, and one of them going unfired is a failure
    rather than a silence. What each row rests on is the runner's own record of
    what this run fired and read back, and the run's own reported check for
    that behaviour — so an executor that reported a check it never made does
    not pass one, and an executor that submitted nothing does not pass by
    silence.
    """
    if acceptance is None:
        return qa_result
    rows = list(acceptance.checks)
    for package in acceptance.activation.packages:
        owned = acceptance.owned[package.name]
        if not owned:
            continue
        declared = acceptance.declared[package.name]
        if not declared:
            rows.append(
                behaviour_check(
                    package,
                    reason=(
                        f"the deployed product declares {', '.join(owned)} for active package "
                        f"{package.name}, and this run's acceptance criteria name no FIRE JOB "
                        "for any of them, so the behaviour was never exercised. QA fires only "
                        "a name a criterion declared, and never invents one"
                    ),
                )
            )
            continue
        rows.extend(
            _behaviour_row(package, criterion, acceptance, workspace, qa_result.checks)
            for criterion in declared
        )
    failed = [row for row in rows if not row["pass"]]
    qa_result.checks = [*rows, *qa_result.checks]
    if not failed:
        return qa_result
    qa_result.passed = False
    qa_result.summary = (
        "the deployed product carries an active kit package whose checks this run did not "
        f"pass: {'; '.join(row['name'] for row in failed)}"
    )
    logger.info("qa_package_acceptance_failed", failed=[row["name"] for row in failed])
    return qa_result


def scheduled_behaviour_facts(
    behaviours: Sequence[ScheduledBehaviourCriterion], *, fireable: bool
) -> list[str]:
    """What the executor is told about this run's named scheduled behaviours.

    The names are not the executor's to choose: the runner read them off this
    run's acceptance criteria, and `fire_job` refuses anything else. Stating
    them here is what lets the checklist ask for the behaviour by name without
    a prompt inviting anyone to guess one.

    The second sentence is the one that keeps the verdict honest. The product's
    core answers a fire with a dispatch record, and `codegen-product-kit`'s own
    contract says a dispatched command is not evidence that a provider consumed
    the event or ran the behaviour. So the fact says, where an executor would
    otherwise read the record as the answer, that the answer is the observable
    the criterion states.
    """
    if not behaviours:
        return []
    if not fireable:
        return [
            "- Scheduled behaviours named by this run's criteria that cannot be fired: "
            + "; ".join(one.name for one in behaviours)
            + ". This deployment holds no jobs capability for the QA runtime, so no fire is "
            "offered. Report each such check as failed with that reason; do not look for "
            "another way to trigger the behaviour, and do not pass it on anything else."
        ]
    return [
        "- Scheduled behaviours this run may invoke, by the name its acceptance criteria "
        "gave them: "
        + "; ".join(f"{one.name} — then: {one.observable}" for one in behaviours)
        + ". Invoke one with `fire_job <name>`; you supply nothing but the name, and only "
        "one of these names. Firing the same name again in this run re-reads the same "
        "single execution instead of causing a second one.",
        "- A fired command comes back with a dispatch record. That record says the "
        "product's core published the event, and nothing more: it is not evidence that "
        "anything consumed it or that the behaviour ran. The check passes only on the "
        "observable stated with the name above — what the product itself sent, wrote or "
        "exposes. `dispatch_status: dispatched` on its own is not a passing check.",
    ]


def confirmed_settings_facts(settings: Sequence[InitialSetting]) -> list[str]:
    """The confirmed brief's typed settings, given to the run as data.

    A story backed by a Product Brief was deployed with these values written
    into the product through its own settings core. An acceptance step about a
    configured behaviour therefore has the value, typed, instead of
    reconstructing it from the story's prose — the canonical case being
    `settings.languages`, where the languages QA asserts on are the confirmed
    ones and not a list read out of a description. A story with no brief adds
    nothing here and the run is exactly as it was.
    """
    if not settings:
        return []
    stated = "; ".join(
        f"{setting.key} (scope {setting.scope.value}"
        + (f", subject {setting.subject_id}" if setting.subject_id is not None else "")
        + f") = {json.dumps(setting.value, ensure_ascii=False)}"
        for setting in settings
    )
    return [
        "- Configured product settings, from the user's confirmed Product Brief and written "
        f"into this deployment by the platform: {stated}. These are the typed confirmed "
        "values, already proved by deployment through privileged seed/readback. Where a check "
        "depends on a configured value, assert against the value here — do not re-derive one "
        "from the story text or from the criteria's wording."
    ]


async def preflight_bot_access(
    *, bot_username: str, telethon_env: dict[str, str] | None
) -> QABlocker | None:
    """Check the platform's own prerequisites for testing a bot, without the LLM.

    The credentials are the QA runtime's, so a missing one is named here rather
    than discovered by the agent mid-run; the probe then asks the bot itself
    whether it admits the QA identity.
    """
    if not telethon_env:
        return QABlocker(
            category=QABlockerCategory.MISSING_TELETHON_CREDENTIALS,
            attempted="validate QA Telethon credentials",
            sent="TELETHON_API_ID, TELETHON_API_HASH, TELETHON_SESSION in the QA runtime",
            received="the QA runtime has no Telegram QA account configured",
        )
    probe = await run_probe_script(
        build_access_probe_script(bot_username),
        env=telethon_env,
        timeout=ACCESS_PROBE_TIMEOUT,
    )
    return classify_access_probe(
        exit_status=probe.exit_status,
        stdout=probe.stdout,
        stderr=probe.stderr,
        bot_username=bot_username,
    )


def _cleanup_blocker(residues: list[str]) -> QABlocker:
    detail = "; ".join(residues)
    return QABlocker(
        category=QABlockerCategory.QA_CLEANUP_FAILED,
        attempted="destroy the QA run's workspace and target access",
        sent="remove the run's authorized_keys entry and its central workspace",
        received=detail,
    )


def _apply_cleanup_residue(qa_result: QAResult, residues: list[str]) -> QAResult:
    """Residual workspace or access turns any QA verdict into a blocker."""
    if not residues:
        return qa_result
    logger.error("qa_cleanup_residual", residual=residues)
    qa_result.passed = False
    qa_result.blocker = _cleanup_blocker(residues)
    qa_result.state_changes = [
        {
            "resource": residue,
            "operation": "created",
            "cleanup": {
                "attempted": True,
                "succeeded": False,
                "detail": residue,
            },
        }
        for residue in residues
    ]
    return qa_result


def _apply_telegram_probe_evidence(qa_result: QAResult, workspace: QAWorkspace) -> QAResult:
    """Use runtime Telegram evidence instead of an executor's unverified verdict."""
    qa_result.telegram_probe_evidence = list(workspace.telegram_probe_evidence)
    if workspace.telegram_probe_blocker is None:
        return qa_result
    qa_result.passed = False
    qa_result.blocker = workspace.telegram_probe_blocker
    qa_result.summary = "QA could not verify the product because a Telegram probe failed"
    return qa_result


async def _invoke_qa_agent(  # noqa: PLR0913 — one run's whole context, each part named
    *,
    target: QATarget,
    ownership: WorkerOwnership,
    workspace: QAWorkspace,
    session,
    acceptance_criteria: str,
    runtime: QARuntimeConfig,
    established_facts: list[str],
    settings_established: bool,
    timeout: int,
    jobs: QAJobsCapability | None,
    acceptance: PackageAcceptance | None = None,
) -> QAResult:
    """Run the one assigned executor over this run's capability endpoint."""
    calls = build_qa_callables(
        session=session,
        workspace=workspace,
        telethon_env=runtime.telethon_env,
        jobs=jobs,
    )
    service = QACapabilityService(
        calls=calls,
        capabilities=session.capabilities.describe(),
        submit_verdict=workspace.submit_verdict,
        advertised_host=runtime.capability_host,
    )
    endpoint = await service.start()
    try:
        executor_run, executor_failure, said = await _run_central_executor(
            target=target,
            ownership=ownership,
            acceptance_criteria=acceptance_criteria,
            runtime=runtime,
            established_facts=established_facts,
            settings_established=settings_established,
            endpoint=endpoint,
            service=service,
            timeout=timeout,
        )
        if executor_run is not None:
            return apply_package_acceptance(
                _apply_telegram_probe_evidence(
                    _verdict_of(workspace, service, timeout, said), workspace
                ),
                acceptance,
                workspace,
            )
    finally:
        await service.stop()

    # QA has exactly one executor. When it does not run there is nothing to fall
    # back to, so the run ends here as infrastructure rather than as a verdict.
    executor = runtime.executor_agent_type.value
    raise QAInfrastructureFailure(
        summary="QA could not be performed: the assigned executor did not run",
        # An executor that started, said something and never called the endpoint
        # leaves that account here and nowhere else: its container is already
        # deleted, and worker-wrapper retains no transcript for a QA executor.
        # Every attempt that ran, not only the last: the last one may be an
        # attempt that never started a container and has nothing to say.
        executor_transcript=said.evidence,
        blocker=QABlocker(
            category=QABlockerCategory.QA_EXECUTOR_UNAVAILABLE,
            attempted=f"run exploratory QA on the assigned executor ({executor})",
            sent=(
                f"{QA_EXECUTOR_ATTEMPTS} start attempt(s) of the {executor} QA executor "
                f"against {target.deployed_url}"
            ),
            received=(
                f"the assigned QA executor ({executor}) did not run: {executor_failure.detail}"
            ),
        ),
    )


async def _run_central_executor(
    *,
    target: QATarget,
    ownership: WorkerOwnership,
    acceptance_criteria: str,
    runtime: QARuntimeConfig,
    established_facts: list[str],
    settings_established: bool,
    endpoint,
    service: QACapabilityService,
    timeout: int,
) -> tuple[QAExecutorRun | None, QAExecutorUnavailable | None, QAExecutorAttempts]:
    """Retry only transient subscription-executor failures.

    Every attempt's transcript is kept, not just the last one's: a first attempt
    that ran and said something, followed by one that never started a container,
    used to return nothing at all. The attempts travel back with the outcome so
    the run they settle retains all of them.
    """
    prepared_criteria = prepare_central_qa_criteria(acceptance_criteria)
    if prepared_criteria.adjustments:
        logger.info(
            "qa_platform_owned_criteria_adjusted",
            qa_run_id=ownership.attempt_id,
            adjustments=[adjustment.as_log() for adjustment in prepared_criteria.adjustments],
        )
    prompt = build_qa_prompt(
        prepared_criteria.criteria,
        target.deployed_url,
        target.bot_username,
        established_facts=established_facts,
        settings_established=settings_established,
    )
    last: QAExecutorUnavailable | None = None
    said = QAExecutorAttempts(QA_EXECUTOR_ATTEMPTS)
    for attempt in range(1, QA_EXECUTOR_ATTEMPTS + 1):
        try:
            run = await run_qa_executor(
                agent_type=runtime.executor_agent_type,
                ownership=ownership,
                capability_url=endpoint.url,
                capability_token=endpoint.token,
                instructions=build_qa_instructions(),
                prompt=prompt,
                verdict_received=service.verdict_received,
                calls_served=lambda: service.calls_served,
                timeout=timeout,
            )
        except QAExecutorUnavailable as exc:
            last = exc
            said = said.with_attempt(attempt, exc.transcript)
            logger.warning(
                "qa_executor_unavailable",
                executor=runtime.executor_agent_type.value,
                attempt=attempt,
                attempts=QA_EXECUTOR_ATTEMPTS,
                transient=exc.transient,
                detail=exc.detail,
            )
            if not exc.transient:
                break
            continue
        logger.info(
            "qa_executor_finished",
            executor=runtime.executor_agent_type.value,
            verdict=run.verdict_submitted,
            calls_served=run.calls_served,
        )
        return run, None, said.with_attempt(attempt, run.transcript)
    return None, last, said


def _verdict_of(
    workspace: QAWorkspace,
    service: QACapabilityService,
    timeout: int,
    said: QAExecutorAttempts,
) -> QAResult:
    """Require a submitted executor verdict before producing QA evidence.

    The evidence is every attempt of this run, not only the one that answered:
    a first attempt that ran and failed transiently said something about why,
    and a second attempt succeeding is not a reason to drop it.
    """
    if workspace.verdict is None:
        return QAResult(
            passed=False,
            summary=f"the QA executor did not submit a result within {timeout}s",
            report=workspace.read_report(),
            executor_evidence=said.evidence,
            blocker=_unknown_result_blocker(
                attempted="run the central QA executor",
                sent=f"{service.calls_served} capability call(s)",
                received="the executor finished without submitting a result",
            ),
        )
    qa_result = parse_qa_result(workspace.verdict)
    qa_result.report = workspace.read_report()
    qa_result.executor_evidence = said.evidence
    return qa_result


class QAProvisioningJournal(Protocol):
    """Record a missing provisioning-owned QA identity against its server."""

    async def missing_identity(self, *, reason: QAIdentityRejection, detail: str) -> None: ...


async def run_qa_centrally(  # noqa: PLR0913 — one run's whole context, each part named
    *,
    target: QATarget,
    ownership: WorkerOwnership,
    fleet_ssh_key: str,
    acceptance_criteria: str,
    runtime: QARuntimeConfig,
    grant_journal: QAGrantJournal,
    provisioning_journal: QAProvisioningJournal,
    established_facts: list[str],
    settings_established: bool = False,
    jobs: QAJobsCapability | None = None,
    timeout: int = QA_TIMEOUT,
) -> QAResult:
    """Run QA with cleanup residue reported as a blocker on every exit path."""
    grant = QAGrantOutcome(marker=new_grant_marker())
    workspace: QAWorkspace | None = None
    try:
        with qa_workspace() as workspace:
            async with qa_target_grant(
                target=target,
                fleet_ssh_key=fleet_ssh_key,
                outcome=grant,
                journal=grant_journal,
            ) as session:
                logger.info(
                    "qa_central_run_started",
                    server_ip=target.server_ip,
                    project_name=target.project_name,
                    timeout=timeout,
                )
                # A failed deterministic container probe does not start an executor.
                container_state = await run_container_state_checks(session)
                if not container_state.passed:
                    logger.info(
                        "qa_container_state_failed_before_agent",
                        server_ip=target.server_ip,
                        summary=container_state.summary,
                    )
                    qa_result = container_state
                else:
                    # What the deployment says about its own kit packages, read
                    # before an executor exists. A product that carries none is
                    # unchanged by this; one whose package contract cannot be
                    # established fails here rather than being judged as if it
                    # had no packages.
                    activation = await run_package_activation_checks(session)
                    if activation.failure is not None:
                        logger.info(
                            "qa_package_contract_unestablished",
                            server_ip=target.server_ip,
                            summary=activation.failure.summary,
                        )
                        qa_result = activation.failure
                    else:
                        # An active package is performed against this deployment,
                        # not described to an executor: the connection check is
                        # read here, and the behaviour it owes is required of the
                        # run's result whatever the executor submits.
                        acceptance = (
                            run_package_acceptance_checks(
                                activation.activation,
                                # What this run's own criteria declared, read here
                                # the same way the fire capability reads it, so
                                # "no criterion named it" and "this deployment
                                # offers no fire" stay two different answers — and
                                # so the observable each criterion states travels
                                # with the name it belongs to.
                                criteria_behaviours=parse_scheduled_behaviours(acceptance_criteria),
                                fireable=jobs is not None,
                            )
                            if activation.activation is not None
                            else None
                        )
                        qa_result = await _invoke_qa_agent(
                            target=target,
                            ownership=ownership,
                            workspace=workspace,
                            session=session,
                            acceptance_criteria=acceptance_criteria,
                            runtime=runtime,
                            established_facts=[
                                *established_facts,
                                container_state_fact(container_state),
                                *(
                                    active_package_facts(
                                        activation.activation,
                                        deployed_url=target.deployed_url,
                                        fireable_behaviours=jobs.names if jobs else (),
                                    )
                                    if activation.activation is not None
                                    else []
                                ),
                            ],
                            settings_established=settings_established,
                            timeout=timeout,
                            jobs=jobs,
                            acceptance=acceptance,
                        )
            # The network is the boundary; scan visible evidence for unexpected writes.
            write = _forbidden_application_write(
                f"{workspace.trace_text()}\n{qa_result.report}\n{qa_result.raw}\n"
                f"{qa_result.executor_evidence or ''}",
                target.deployed_url,
            )
            if write:
                qa_result = _block_forbidden_application_write(qa_result, write)
    except (QAInfrastructureFailure, QAContainerRuntimeError) as exc:
        # Executor and runtime failures are infrastructure blockers, never product verdicts.
        failure = (
            exc
            if isinstance(exc, QAInfrastructureFailure)
            else container_runtime_unavailable(
                sent=f"docker ps of compose project {target.project_name} on {target.server_ip}",
                received=str(exc),
            )
        )
        logger.error(
            "qa_infrastructure_failure",
            server_ip=target.server_ip,
            category=failure.blocker.category.value,
            attempted=failure.blocker.attempted,
            detail=failure.blocker.received,
        )
        return _apply_cleanup_residue(
            QAResult(
                passed=False,
                summary=failure.summary,
                blocker=failure.blocker,
                executor_evidence=failure.executor_transcript,
            ),
            _residues(grant, workspace),
        )
    except (QAGrantError, QACapabilityError) as exc:
        logger.error("qa_grant_failed", server_ip=target.server_ip, error=str(exc))
        if isinstance(exc, QAIdentityAbsentError):
            # Missing target identity belongs in the provisioning journal.
            await provisioning_journal.missing_identity(
                reason=QAIdentityRejection.ABSENT_ON_TARGET,
                detail=str(exc),
            )
        # A permission problem on the target is named as one. It is not
        # `server_unavailable`: the run reached the host, and the repair is the
        # server row's administrative account rather than the host's
        # provisioning.
        category = (
            QABlockerCategory.QA_IDENTITY_UNREADABLE
            if isinstance(exc, QAIdentityUnreadableError)
            else QABlockerCategory.SERVER_UNAVAILABLE
        )
        # An unconfirmed grant remains cleanup residue.
        return _apply_cleanup_residue(
            QAResult(
                passed=False,
                summary=f"QA could not obtain access to {target.server_ip}: {exc}",
                blocker=QABlocker(
                    category=category,
                    attempted="issue a one-shot QA identity on the target",
                    sent=f"authorized_keys entry {grant.marker} on {target.server_ip}",
                    received=str(exc),
                ),
            ),
            _residues(grant, workspace),
        )
    except Exception as exc:
        logger.exception("qa_central_run_failed", server_ip=target.server_ip)
        return _apply_cleanup_residue(
            QAResult(
                passed=False,
                summary=f"QA run against {target.server_ip} failed: {exc}",
                blocker=_unknown_result_blocker(
                    attempted="run the central QA agent against the target",
                    sent=f"QA run on {target.server_ip}",
                    received=str(exc),
                ),
            ),
            _residues(grant, workspace),
        )

    return _apply_cleanup_residue(qa_result, _residues(grant, workspace))


def _residues(grant: QAGrantOutcome, workspace: QAWorkspace | None) -> list[str]:
    """Everything this run created and could not prove gone."""
    return [
        residue
        for residue in (grant.residual, workspace.residual if workspace else None)
        if residue
    ]

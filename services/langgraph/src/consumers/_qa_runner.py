"""Run deterministic QA checks, then subscription executor, then optional API fallback."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
import json
import re
from typing import Protocol
import uuid

import asyncssh
import httpx
import structlog

from shared.contracts.acceptance import HealthCriterion, ScheduledBehaviourCriterion
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

from ..agents.qa.capability_service import QACapabilityService
from ..agents.qa.graph import create_qa_graph
from ..agents.qa.tools import QAJobsCapability, build_qa_callables, build_qa_tools
from ..clients.qa_worker import QAExecutorRun, QAExecutorUnavailable, run_qa_executor
from ..config.agent_llm_env import AGENT_LLM_ENV
from ..prompts.qa import QAExecutorKind, build_qa_instructions, build_qa_prompt
from ._qa_target import (
    CONTAINER_PROBE_ATTEMPTS,
    CONTAINER_PROBE_RETRY_DELAY,
    QACapabilityError,
    QAContainerRuntimeError,
    QAGrantError,
    QAGrantJournal,
    QAGrantOutcome,
    QAIdentityAbsentError,
    QATarget,
    new_grant_marker,
    qa_target_grant,
)
from ._qa_workspace import QAWorkspace, qa_workspace

logger = structlog.get_logger(__name__)

QA_TIMEOUT = 1200  # 20 minutes
QA_MAX_STEPS = 200
# Retry only transient subscription-executor failures.
QA_EXECUTOR_ATTEMPTS = 2
HEALTH_CHECK_TIMEOUT = 30
HEALTH_CHECK_ATTEMPTS = 5
HEALTH_CHECK_RETRY_DELAY = 5
ACCESS_PROBE_TIMEOUT = 60
CONTAINER_HEALTHY = "healthy"
_WRITE_METHODS = "POST|PUT|PATCH|DELETE"


@dataclass(frozen=True)
class QARuntimeConfig:
    """Assigned executor and management-host capability/Telegram configuration."""

    executor_agent_type: AgentType
    capability_host: str
    telethon_env: dict[str, str] | None = None


@dataclass(frozen=True)
class QAApiFallback:
    """The optional API triplet, resolved only after the assigned executor failed."""

    model: str
    base_url: str
    api_key: str


class QAInfrastructureFailure(Exception):
    """Typed infrastructure failure that must not become a product verdict."""

    def __init__(self, *, summary: str, blocker: QABlocker) -> None:
        super().__init__(blocker.received)
        self.summary = summary
        self.blocker = blocker


def api_fallback(settings) -> QAApiFallback | None:
    """Resolve API fallback only after the subscription executor failed."""
    model, base_url, api_key = (getattr(settings, name.lower()) for name in AGENT_LLM_ENV["qa"])
    if model and base_url and api_key:
        return QAApiFallback(model=model, base_url=base_url, api_key=api_key)
    return None


def missing_api_fallback_env(settings) -> list[str]:
    """Which of the fallback triplet's names carry no value, for the admin alert."""
    return [name for name in AGENT_LLM_ENV["qa"] if not getattr(settings, name.lower())]


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
    # Executor transcript is scanned with runner-owned evidence for forbidden writes.
    executor_evidence: str = ""


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


def scheduled_behaviour_facts(
    behaviours: Sequence[ScheduledBehaviourCriterion], *, fireable: bool
) -> list[str]:
    """What the executor is told about this run's named scheduled behaviours.

    The names are not the executor's to choose: the runner read them off this
    run's acceptance criteria, and `fire_job` refuses anything else. Stating
    them here is what lets the checklist ask for the behaviour by name without
    a prompt inviting anyone to guess one.

    The second sentence is the one that keeps the verdict honest. The product's
    core answers a fire with a dispatch record, and `service-template`'s own
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
        "values. Where a check depends on a configured value, assert against the value "
        "here — do not re-derive one from the story text or from the criteria's wording."
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


def _final_message_text(state: dict) -> str:
    """The agent's last message, as text.

    Chat models may return content as a list of blocks; a QA result buried in
    one of them is still the result, so the blocks are joined rather than
    stringified into `[{'type': ...}]`.
    """
    messages = state.get("messages") or []
    if not messages:
        return ""
    content = getattr(messages[-1], "content", messages[-1])
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return content if isinstance(content, str) else str(content)


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


async def _invoke_qa_agent(
    *,
    target: QATarget,
    ownership: WorkerOwnership,
    workspace: QAWorkspace,
    session,
    acceptance_criteria: str,
    runtime: QARuntimeConfig,
    established_facts: list[str],
    timeout: int,
    settings,
    jobs: QAJobsCapability | None,
) -> QAResult:
    """Run the assigned executor, then optional API fallback through one endpoint."""
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
        executor_run, executor_failure = await _run_central_executor(
            target=target,
            ownership=ownership,
            acceptance_criteria=acceptance_criteria,
            runtime=runtime,
            established_facts=established_facts,
            endpoint=endpoint,
            service=service,
            timeout=timeout,
        )
        if executor_run is not None:
            return _apply_telegram_probe_evidence(
                _verdict_of(workspace, service, executor_run, timeout), workspace
            )
    finally:
        await service.stop()

    fallback = api_fallback(settings)
    if fallback is None:
        missing = missing_api_fallback_env(settings)
        raise QAInfrastructureFailure(
            summary="QA could not be performed: no executor was available",
            blocker=QABlocker(
                category=QABlockerCategory.QA_EXECUTOR_UNAVAILABLE,
                attempted=(
                    f"run exploratory QA on the assigned executor "
                    f"({runtime.executor_agent_type.value})"
                ),
                sent=", ".join(missing) or "no API fallback configured",
                received=(
                    f"the assigned QA executor ({runtime.executor_agent_type.value}) did not run "
                    f"({executor_failure.detail}), and no API fallback is configured"
                ),
            ),
        )
    logger.warning(
        "qa_executor_fallback_to_api",
        executor=runtime.executor_agent_type.value,
        detail=executor_failure.detail,
    )
    return _apply_telegram_probe_evidence(
        await _run_in_process_agent(
            target=target,
            workspace=workspace,
            session=session,
            acceptance_criteria=acceptance_criteria,
            runtime=runtime,
            established_facts=established_facts,
            fallback=fallback,
            timeout=timeout,
            jobs=jobs,
        ),
        workspace,
    )


async def _run_central_executor(
    *,
    target: QATarget,
    ownership: WorkerOwnership,
    acceptance_criteria: str,
    runtime: QARuntimeConfig,
    established_facts: list[str],
    endpoint,
    service: QACapabilityService,
    timeout: int,
) -> tuple[QAExecutorRun | None, QAExecutorUnavailable | None]:
    """Retry only transient subscription-executor failures."""
    prompt = build_qa_prompt(
        acceptance_criteria,
        target.deployed_url,
        target.bot_username,
        executor=QAExecutorKind.CENTRAL_AGENT,
        established_facts=established_facts,
    )
    last: QAExecutorUnavailable | None = None
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
        return run, None
    return None, last


def _verdict_of(
    workspace: QAWorkspace,
    service: QACapabilityService,
    executor_run: QAExecutorRun,
    timeout: int,
) -> QAResult:
    """Require a submitted executor verdict before producing QA evidence."""
    if workspace.verdict is None:
        return QAResult(
            passed=False,
            summary=f"the QA executor did not submit a result within {timeout}s",
            report=workspace.read_report(),
            executor_evidence=executor_run.transcript,
            blocker=_unknown_result_blocker(
                attempted="run the central QA executor",
                sent=f"{service.calls_served} capability call(s)",
                received="the executor finished without submitting a result",
            ),
        )
    qa_result = parse_qa_result(workspace.verdict)
    qa_result.report = workspace.read_report()
    qa_result.executor_evidence = executor_run.transcript
    return qa_result


async def _run_in_process_agent(
    *,
    target: QATarget,
    workspace: QAWorkspace,
    session,
    acceptance_criteria: str,
    runtime: QARuntimeConfig,
    established_facts: list[str],
    fallback: QAApiFallback,
    timeout: int,
    jobs: QAJobsCapability | None,
) -> QAResult:
    """The API fallback: the ReactAgent this used to be, over the same tool set."""
    tools = build_qa_tools(
        session=session,
        workspace=workspace,
        telethon_env=runtime.telethon_env,
        jobs=jobs,
    )
    graph = create_qa_graph(
        model=fallback.model,
        base_url=fallback.base_url,
        api_key=fallback.api_key,
        tools=tools,
        prompt=build_qa_prompt(
            acceptance_criteria,
            target.deployed_url,
            target.bot_username,
            executor=QAExecutorKind.IN_PROCESS_TOOLS,
            established_facts=established_facts,
        ),
    )
    try:
        state = await asyncio.wait_for(
            graph.ainvoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "Run the regression test now and finish with the result JSON."
                            ),
                        }
                    ]
                },
                config={
                    "recursion_limit": QA_MAX_STEPS,
                    "configurable": {"thread_id": str(uuid.uuid4())},
                },
            ),
            timeout=timeout,
        )
    except TimeoutError:
        logger.warning("qa_agent_timeout", server_ip=target.server_ip, timeout=timeout)
        return QAResult(
            passed=False,
            summary=f"QA agent did not finish within {timeout}s",
            report=workspace.read_report(),
            blocker=_unknown_result_blocker(
                attempted="run the central QA agent",
                sent=f"QA agent against {target.deployed_url}",
                received=f"no result after {timeout}s",
            ),
        )

    qa_result = parse_qa_result(_final_message_text(state))
    qa_result.report = workspace.read_report()
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
    settings,
    established_facts: list[str],
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
                        ],
                        timeout=timeout,
                        settings=settings,
                        jobs=jobs,
                    )
            # The network is the boundary; scan visible evidence for unexpected writes.
            write = _forbidden_application_write(
                f"{workspace.trace_text()}\n{qa_result.report}\n{qa_result.raw}\n"
                f"{qa_result.executor_evidence}",
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
            QAResult(passed=False, summary=failure.summary, blocker=failure.blocker),
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
        # An unconfirmed grant remains cleanup residue.
        return _apply_cleanup_residue(
            QAResult(
                passed=False,
                summary=f"QA could not obtain access to {target.server_ip}: {exc}",
                blocker=QABlocker(
                    category=QABlockerCategory.SERVER_UNAVAILABLE,
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

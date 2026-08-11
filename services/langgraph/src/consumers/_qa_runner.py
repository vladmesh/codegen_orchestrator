"""QA runners — HTTP checks for criteria we can decide, a central agent for the rest.

Criteria that only state GET expectations are run directly against the deployed
URL by `run_health_checks`. Anything else goes to `run_qa_centrally`, which runs
the QA agent here, in the orchestrator, and lets it reach the deployment only
through the typed tools in `agents/qa/tools`.

Nothing in this module puts an agent, an LLM credential or a Telegram session on
the deploy target. The target sees one short-lived SSH identity issued for the
run and a closed set of read-only calls; see `_qa_target`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import re
from typing import Protocol
import uuid

import httpx
import structlog

from shared.contracts.acceptance import HealthCriterion
from shared.contracts.dto.run_result import (
    QABlocker,
    QABlockerCategory,
)
from shared.qa_identity import QAIdentityRejection
from shared.telegram_access_probe import (
    build_access_probe_script,
    classify_access_probe,
    run_probe_script,
)

from ..agents.qa.graph import create_qa_graph
from ..agents.qa.tools import build_qa_tools
from ..prompts.qa import build_qa_prompt
from ._qa_target import (
    QACapabilityError,
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
HEALTH_CHECK_TIMEOUT = 30
HEALTH_CHECK_ATTEMPTS = 5
HEALTH_CHECK_RETRY_DELAY = 5
ACCESS_PROBE_TIMEOUT = 60
_WRITE_METHODS = "POST|PUT|PATCH|DELETE"


@dataclass(frozen=True)
class QARuntimeConfig:
    """What the central QA agent needs to exist: an LLM, and a QA Telegram account.

    `telethon_env` is the QA account's credentials. They live in the QA
    runtime's environment and are handed to a probe child process; the agent
    never receives them and no deploy target holds a copy.
    """

    model: str
    base_url: str
    api_key: str
    telethon_env: dict[str, str] | None = None


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
    """Validate every routing-relevant field in a QA agent response.

    A malformed result is not product evidence. It must be routed to human
    review as an unknown blocker instead of being treated as a pass or causing
    failure handling to crash while extracting failed checks.
    """
    required_fields = {"pass", "checks", "summary"}
    # Older agents may still emit state_changes. It is deliberately ignored:
    # cleanup evidence is produced by the runner, not trusted agent output.
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
    """Parse the QA agent's final message into a QAResult.

    Handles:
    - Raw QA JSON: {"pass": true, ...}
    - JSON wrapped in markdown code blocks
    - A CLI-style {"type":"result","result":"..."} wrapper, still accepted so a
      transcript captured from the previous runtime parses the same way
    """
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

    # Step 1: Unwrap a result wrapper if present
    try:
        wrapper = json.loads(json_str)
        if isinstance(wrapper, dict) and wrapper.get("type") == "result":
            # Extract the inner result text
            json_str = wrapper.get("result", "")
    except json.JSONDecodeError:
        pass  # Not a wrapper, continue with raw

    # Step 2: Extract JSON from markdown code blocks
    code_block_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", json_str, re.DOTALL)
    if code_block_match:
        json_str = code_block_match.group(1).strip()

    # Step 3: Parse as QA result
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
    """A run whose materials outlived it is not a clean pass.

    The run's own verdict is kept in the summary, but the outcome becomes a
    blocker: leftover access is a platform fact a human has to clear, and it
    must not be reported as a green QA run.
    """
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


async def _invoke_qa_agent(
    *,
    target: QATarget,
    workspace: QAWorkspace,
    session,
    acceptance_criteria: str,
    runtime: QARuntimeConfig,
    timeout: int,
) -> QAResult:
    """Run the agent against one target and turn its answer into a QAResult."""
    tools = build_qa_tools(
        session=session,
        workspace=workspace,
        telethon_env=runtime.telethon_env,
    )
    graph = create_qa_graph(
        model=runtime.model,
        base_url=runtime.base_url,
        api_key=runtime.api_key,
        tools=tools,
        prompt=build_qa_prompt(acceptance_criteria, target.deployed_url, target.bot_username),
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
    """Where "this host has no QA identity" is recorded against the server.

    The same fact reaches this runtime two ways: off the server row, before
    anything connects, and off the target itself, when the account the row
    promised turns out not to be there. Both are provisioning facts about the
    host and both belong in the same journal entry, so the runner is handed the
    journal rather than deciding on its own that a failure it met halfway
    through is only this run's problem.
    """

    async def missing_identity(self, *, reason: QAIdentityRejection, detail: str) -> None: ...


async def run_qa_centrally(
    *,
    target: QATarget,
    fleet_ssh_key: str,
    acceptance_criteria: str,
    runtime: QARuntimeConfig,
    grant_journal: QAGrantJournal,
    provisioning_journal: QAProvisioningJournal,
    timeout: int = QA_TIMEOUT,
) -> QAResult:
    """Run exploratory QA from the orchestrator against one deployment.

    The run gets an isolated workspace here and a one-shot SSH identity there.
    Both are destroyed before this returns, on every path out — a raised error,
    a timeout, a cancelled run — and what could not be destroyed is reported as
    a blocker on every one of those paths, including the early return when the
    identity could not be issued at all. That last case is the one that used to
    be silent: an install whose answer was lost may have landed, so it is
    residue until something reads the target back.

    Args:
        target: the single deployment this run may reach.
        fleet_ssh_key: the server key, used by this function only to issue and
            revoke the run's own identity. It is never given to the agent.
        acceptance_criteria: regression test criteria from the repository.
        runtime: LLM configuration and the QA Telegram account credentials.
        grant_journal: where the durable record of the grant is written.
        provisioning_journal: where a target that turns out to have no QA
            account is recorded, so drift after a finished provisioning is as
            visible to an administrator as a host that was never provisioned.
        timeout: seconds the agent is given to reach a verdict.
    """
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
                qa_result = await _invoke_qa_agent(
                    target=target,
                    workspace=workspace,
                    session=session,
                    acceptance_criteria=acceptance_criteria,
                    runtime=runtime,
                    timeout=timeout,
                )
            # Everything the runner can see of the run: the calls it made on the
            # agent's behalf, the report, and the agent's own account of itself.
            write = _forbidden_application_write(
                f"{workspace.trace_text()}\n{qa_result.report}\n{qa_result.raw}",
                target.deployed_url,
            )
            if write:
                qa_result = _block_forbidden_application_write(qa_result, write)
    except (QAGrantError, QACapabilityError) as exc:
        logger.error("qa_grant_failed", server_ip=target.server_ip, error=str(exc))
        if isinstance(exc, QAIdentityAbsentError):
            # The row says this host has an account and the host says otherwise.
            # That is the same missing identity the label check refuses before a
            # connection is opened, found one step later, so it goes to the same
            # place: an administrator looking at this server sees one entry
            # naming the handle and the repair, not a QA blocker in a log.
            await provisioning_journal.missing_identity(
                reason=QAIdentityRejection.ABSENT_ON_TARGET,
                detail=str(exc),
            )
        # The residue goes through the same path as every other exit: an
        # unconfirmed install is access this run may be holding, and a run that
        # reports only "server unavailable" hides it.
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

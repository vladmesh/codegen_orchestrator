"""The complete reach of a central QA run.

Every call here is built for one run and closes over that run's target session,
workspace and Telegram credentials. There is no module-level state and no
parameter naming a host, so an agent cannot address a second deployment: the
only target it can reach is the one the runner bound these calls to.

None of these can write to the application's own data. The HTTP calls take no
method, the remote calls take an allowlist-checked argument vector, and the
Telegram call sends a message the platform's own test account is entitled to
send.

``fire_job`` is the one call that asks the product to *do* something, and it is
bounded the same way rather than by trust: it names a behaviour the run's own
acceptance criteria declared, carries arguments those criteria stated, and fires
under an identity the runner owns, which the product bounds execution on. It
reaches no application endpoint of the product's own domain, and what it returns
is a dispatch record — never a verdict. The template's contract says in those
words that a dispatched command is not evidence a provider ran the behaviour, so
the answer carries that sentence and the judgement stays with the behaviour's
own output.

There are two front-ends over one boundary, and only one boundary.
:func:`build_qa_callables` is it. :func:`build_qa_tools` wraps the callables as
LangChain tools for the in-process fallback agent, and
``agents/qa/capability_service`` serves the same dictionary over HTTP to the
central executor container. Neither front-end may add an operation, widen an
argument, or decide anything the capability set does not decide — a call the
executor makes is the same call the fallback agent makes.

A refused call comes back as an ``error`` field rather than an exception: the
agent has to be able to read "that is out of scope" and choose another check,
and the runner keeps its own record of the refusal either way.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import httpx
from langchain_core.tools import StructuredTool
import structlog

from shared.contracts.acceptance import ScheduledBehaviourCriterion
from shared.contracts.dto.run_result import (
    QABlocker,
    QABlockerCategory,
    QATelegramProbeEvidence,
)
from shared.telegram_access_probe import ProbeRun, run_probe_script
from shared.telegram_bot_probe import (
    TELEGRAM_REPLY_TIMEOUT,
    build_bot_callback_script,
    build_bot_message_script,
    parse_bot_probe_result,
)

from ...clients.product_jobs import (
    DISPATCH_IS_NOT_PROOF,
    GeneratedServiceJobsClient,
    JobCallFailure,
    JobCallOutcome,
)
from ...consumers._qa_target import QACapabilities, QATargetError, QATargetSession
from ...consumers._qa_workspace import QAWorkspace

logger = structlog.get_logger(__name__)

PUBLIC_PROBE_TIMEOUT = 30
MAX_BODY = 8000


def _truncate(text: str) -> str:
    return text[:MAX_BODY]


def _remote_tools(session: QATargetSession, record, refuse) -> dict:
    """The calls that leave the QA runtime, each bounded by the capability set.

    None of these carries a rule of its own: `http_get` can only address the
    deployed URL in the set, `localhost_http_get` only a port in it,
    `remote_read` only what resolves inside its physical root, and the docker
    calls only a container that is in it.
    """
    capabilities = session.capabilities

    async def http_get(path: str) -> dict:
        base = capabilities.deployed_url.rstrip("/")
        url = f"{base}{path if path.startswith('/') else '/' + path}"
        try:
            async with httpx.AsyncClient(
                timeout=PUBLIC_PROBE_TIMEOUT, follow_redirects=False
            ) as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            record("http_get", url, f"transport error: {exc}")
            return {"error": f"transport error: {exc}", "url": url}
        result = {
            "url": url,
            "status": response.status_code,
            "headers": dict(response.headers),
            "body": _truncate(response.text),
        }
        record("http_get", f"GET {url}", f"{response.status_code} {result['body']}")
        return result

    async def localhost_http_get(port: int, path: str) -> dict:
        request = f"GET http://127.0.0.1:{port}{path}"
        try:
            remote = await session.localhost_http_get(port, path)
        except QATargetError as exc:
            return refuse("localhost_http_get", request, exc)
        record("localhost_http_get", request, remote.stdout or remote.stderr)
        return remote.as_dict()

    async def remote_read(path: str) -> dict:
        try:
            remote = await session.read_file(path)
        except QATargetError as exc:
            return refuse("remote_read", path, exc)
        record("remote_read", path, remote.stdout or remote.stderr)
        return remote.as_dict()

    async def remote_exec(command: list[str]) -> dict:
        request = " ".join(command)
        try:
            remote = await session.exec(command)
        except QATargetError as exc:
            return refuse("remote_exec", request, exc)
        record("remote_exec", request, remote.stdout or remote.stderr)
        return remote.as_dict()

    async def container_logs(container: str, tail: int = 200) -> dict:
        try:
            remote = await session.container_logs(container, tail=tail)
        except QATargetError as exc:
            return refuse("container_logs", container, exc)
        record("container_logs", f"{container} tail={tail}", remote.stdout or remote.stderr)
        return remote.as_dict()

    async def container_inspect(container: str) -> dict:
        try:
            remote = await session.container_inspect(container)
        except QATargetError as exc:
            return refuse("container_inspect", container, exc)
        record("container_inspect", container, remote.stdout or remote.stderr)
        return remote.as_dict()

    return {
        "http_get": http_get,
        "localhost_http_get": localhost_http_get,
        "remote_read": remote_read,
        "remote_exec": remote_exec,
        "container_logs": container_logs,
        "container_inspect": container_inspect,
    }


class _TelegramCapability:
    """One run's Telegram operations and the callback data they made visible."""

    def __init__(
        self,
        *,
        bot_username: str,
        workspace: QAWorkspace,
        telethon_env: dict[str, str],
        probe_runner: Callable[..., object],
    ) -> None:
        self._bot_username = bot_username
        self._workspace = workspace
        self._telethon_env = telethon_env
        self._run_probe = probe_runner
        self._visible_callbacks: dict[tuple[int, str], str] = {}

    def _record_evidence(self, tool: str, evidence: QATelegramProbeEvidence) -> dict:
        """Persist runner-owned evidence and fail closed on Telegram errors."""
        blocker = self._blocker_for(evidence)
        self._workspace.record_telegram_probe(evidence, blocker)
        serialized = evidence.model_dump(mode="json")
        self._workspace.record(tool, evidence.attempted, repr(serialized))
        return {"error": evidence.error, **serialized} if blocker else serialized

    @staticmethod
    def _blocker_for(evidence: QATelegramProbeEvidence) -> QABlocker | None:
        if not evidence.error:
            return None
        category = (
            QABlockerCategory.TELEGRAM_PROBE_UNDELIVERED
            if evidence.delivered is False
            else QABlockerCategory.UNKNOWN
        )
        return QABlocker(
            category=category,
            attempted=evidence.attempted,
            sent=evidence.sent,
            received=evidence.error,
        )

    def _unparseable_evidence(
        self, *, action: str, attempted: str, sent: str, error: str, tool: str
    ) -> dict:
        """A child process with no usable result is not product evidence either."""
        return self._record_evidence(
            tool,
            QATelegramProbeEvidence(
                action=action,
                attempted=attempted,
                sent=sent,
                delivered=None,
                error=error,
            ),
        )

    def _remember_visible_callbacks(self, evidence: QATelegramProbeEvidence) -> None:
        for reply in evidence.replies:
            if reply.reply_markup:
                for button in reply.reply_markup.buttons:
                    if button.callback_data:
                        self._visible_callbacks[(reply.id, button.callback_data)] = (
                            button.text or "inline button"
                        )

    def _parse_result(
        self,
        run: ProbeRun,
        *,
        action: str,
        attempted: str,
        sent: str,
        tool: str,
    ) -> dict:
        try:
            evidence = QATelegramProbeEvidence.model_validate(parse_bot_probe_result(run.stdout))
        except ValueError as exc:
            detail = (run.stderr or run.stdout or str(exc)).strip()[-1000:]
            return self._unparseable_evidence(
                action=action,
                attempted=attempted,
                sent=sent,
                error=f"Telegram {action} probe returned no usable evidence: {detail}",
                tool=tool,
            )
        self._remember_visible_callbacks(evidence)
        return self._record_evidence(tool, evidence)

    async def telegram_probe(self, message: str) -> dict:
        # Telegram cannot carry an empty or whitespace-only message: the API
        # rejects it before it reaches the product, so the attempt says nothing
        # about the bot. Refusing here, with no error on the evidence, keeps the
        # checklist's "empty input" item from turning a working deploy into a
        # blocked QA run — which is what it did to two users' bots on
        # 2026-08-27. The agent gets a plain answer and moves to the next check.
        if not message.strip():
            attempted = f"send {message!r} to @{self._bot_username}"
            evidence = QATelegramProbeEvidence(
                action="message",
                attempted=attempted,
                sent=message,
                delivered=False,
                replies=[],
            )
            logger.info(
                "qa_tool_refused",
                tool="telegram_probe",
                reason="empty_message_unsupported_by_transport",
                bot=self._bot_username,
            )
            self._workspace.record_telegram_probe(evidence, None)
            self._workspace.record("telegram_probe", attempted, "empty message not sendable")
            return {
                **evidence.model_dump(mode="json"),
                "not_applicable": (
                    "Telegram rejects an empty message before delivery, so this check "
                    "cannot be performed over this transport. It is not a product defect."
                ),
            }

        run: ProbeRun = await self._run_probe(
            build_bot_message_script(self._bot_username, message),
            env=self._telethon_env,
            timeout=TELEGRAM_REPLY_TIMEOUT + 30,
        )
        return self._parse_result(
            run,
            action="message",
            attempted=f"send {message!r} to @{self._bot_username}",
            sent=message,
            tool="telegram_probe",
        )

    async def telegram_click_button(self, message_id: int, callback_data: str) -> dict:
        """Invoke exactly one inline button a prior reply made visible in this run."""
        button_text = self._visible_callbacks.get((message_id, callback_data))
        sent = f"message_id={message_id} callback_data={callback_data}"
        if button_text is None:
            error = "the callback is not from an inline button visible in this run's bot replies"
            logger.info("qa_tool_refused", tool="telegram_click_button", error=error)
            return self._record_evidence(
                "telegram_click_button",
                QATelegramProbeEvidence(
                    action="callback",
                    attempted="press a callback requested by the executor",
                    sent=sent,
                    delivered=False,
                    error=error,
                ),
            )
        run: ProbeRun = await self._run_probe(
            build_bot_callback_script(
                self._bot_username,
                message_id,
                callback_data,
                button_text=button_text,
            ),
            env=self._telethon_env,
            timeout=TELEGRAM_REPLY_TIMEOUT + 30,
        )
        return self._parse_result(
            run,
            action="callback",
            attempted=f"press {button_text}",
            sent=sent,
            tool="telegram_click_button",
        )


@dataclass(frozen=True)
class QAJobsCapability:
    """What one QA run may fire, and under whose identity it fires it.

    Assembled on the management host before any executor exists. The
    `capability` is the deployment's generated `JOBS_FIRE_CAPABILITY`: it is
    read here, put in one request header by the client, and never travels into
    the executor container, its environment, its arguments, the trace, a log
    line or a verdict.

    `behaviours` is the closed set of names this run may fire, read off the
    run's own acceptance criteria. An executor cannot widen it and cannot
    invent a name, because it never supplies the arguments and never supplies a
    name that is not in this set.
    """

    base_url: str
    capability: str
    fired_by_product: str
    fired_by_run: str
    behaviours: tuple[ScheduledBehaviourCriterion, ...]

    def command_id(self, name: str) -> str:
        """The identity this run fires `name` under, every time it fires it.

        One identity per (run, behaviour). The product bounds execution on
        `(fired_by_product, command_id)`, so a second call within this run —
        a retry, or the same logical check reached twice — returns the recorded
        evidence and emits nothing. A retry of the call is therefore safe by
        construction rather than by the caller being careful.
        """
        return f"qa-{self.fired_by_run}-{name}"

    def behaviour(self, name: str) -> ScheduledBehaviourCriterion | None:
        return next((one for one in self.behaviours if one.name == name), None)

    @property
    def names(self) -> list[str]:
        return [one.name for one in self.behaviours]


#: What each closed-set failure means, in the words a QA executor has to be
#: able to act on. A refusal is an answer, never a crash: the executor reads it
#: and decides what to do with the check it was making.
_JOB_CALL_ERRORS = {
    JobCallFailure.NAME_NOT_DECLARED: (
        "the product declares no scheduled behaviour by this name, so nothing was fired "
        "(its jobs core answered 404). The behaviour this check needs does not exist in "
        "the deployment under test."
    ),
    JobCallFailure.ARGUMENTS_REJECTED: (
        "the product refused the arguments this check declares for the behaviour "
        "(its jobs core answered 422). Nothing was fired."
    ),
    JobCallFailure.NO_COMMAND_RECORDED: (
        "the product has no recorded command under this run's identity for that "
        "behaviour. Fire it first; evidence exists only for a command that was fired."
    ),
    JobCallFailure.REJECTED: "the product refused the call.",
    JobCallFailure.TRANSPORT: "the product's jobs core could not be reached.",
    JobCallFailure.MALFORMED_ANSWER: (
        "the product answered with something that is not a recorded job command, so "
        "there is no evidence to read."
    ),
}


class _JobsCapability:
    """One run's two jobs calls: invoke a named behaviour, and read it back.

    Neither call names a module, a queue, a container or a transport, and
    neither takes arguments from the executor: the name is checked against the
    run's declared set and the arguments come from the criterion that declared
    it. What comes back never contains the capability and never contains the
    deployment URL — the runner's own write guard reads this trace, and the
    sanctioned fire must not be spelled the way a forbidden direct write is.
    """

    def __init__(
        self,
        *,
        jobs: QAJobsCapability,
        workspace: QAWorkspace,
        client_factory: Callable[[str], GeneratedServiceJobsClient] | None = None,
    ) -> None:
        self._jobs = jobs
        self._workspace = workspace
        self._client_factory = client_factory or GeneratedServiceJobsClient

    async def fire_job(self, name: str) -> dict:
        """Invoke one declared scheduled behaviour on the deployment under test."""
        behaviour = self._jobs.behaviour(name)
        if behaviour is None:
            return self._undeclared("fire_job", name)
        outcome = await self._client_factory(self._jobs.base_url).fire(
            command_id=self._jobs.command_id(name),
            name=behaviour.name,
            arguments=behaviour.arguments,
            fired_by_product=self._jobs.fired_by_product,
            fired_by_run=self._jobs.fired_by_run,
            capability=self._jobs.capability,
        )
        return self._answer("fire_job", behaviour, outcome)

    async def job_evidence(self, name: str) -> dict:
        """Read back what the product recorded for this run's fire of `name`."""
        behaviour = self._jobs.behaviour(name)
        if behaviour is None:
            return self._undeclared("job_evidence", name)
        outcome = await self._client_factory(self._jobs.base_url).evidence(
            command_id=self._jobs.command_id(name),
            fired_by_product=self._jobs.fired_by_product,
        )
        return self._answer("job_evidence", behaviour, outcome)

    def _undeclared(self, tool: str, name: str) -> dict:
        """A name this run's criteria did not declare is refused here, not fired.

        This is the whole of "never guessed": the platform reached the name by
        reading a checklist line, and a name that came from anywhere else has
        no fire to make.
        """
        error = (
            f"{name!r} is not a scheduled behaviour this run's acceptance criteria named; "
            f"this run may fire: {', '.join(self._jobs.names) or '(none)'}"
        )
        logger.info("qa_tool_refused", tool=tool, error=error)
        self._workspace.record(tool, f"{tool} {name}", f"refused: {error}")
        return {"error": error, "declared_behaviours": self._jobs.names}

    def _answer(
        self, tool: str, behaviour: ScheduledBehaviourCriterion, outcome: JobCallOutcome
    ) -> dict:
        command_id = self._jobs.command_id(behaviour.name)
        request = f"{tool} {behaviour.name} command_id={command_id}"
        if outcome.command is None:
            failure = outcome.failure or JobCallFailure.MALFORMED_ANSWER
            error = _JOB_CALL_ERRORS[failure]
            logger.info("qa_job_call_failed", tool=tool, name=behaviour.name, failure=failure.value)
            self._workspace.record(tool, request, f"{failure.value}: {error}")
            return {
                "error": error,
                "failure": failure.value,
                "name": behaviour.name,
                "command_id": command_id,
            }
        answer = {
            **outcome.command.as_dict(),
            "observable": behaviour.observable,
            "dispatch_is_not_proof": DISPATCH_IS_NOT_PROOF,
        }
        self._workspace.record(tool, request, repr(answer))
        return answer


def build_qa_callables(
    *,
    session: QATargetSession,
    workspace: QAWorkspace,
    telethon_env: dict[str, str] | None = None,
    probe_runner: Callable[..., object] | None = None,
    jobs: QAJobsCapability | None = None,
    jobs_client_factory: Callable[[str], GeneratedServiceJobsClient] | None = None,
) -> dict[str, Callable]:
    """Build the whole reach of exactly one QA run, keyed by call name.

    This is the boundary. Whatever front-end an executor speaks — LangChain
    tools in this process, or the HTTP capability endpoint a container calls —
    it dispatches into this dictionary and can neither add to it nor reach past
    it.

    Args:
        session: the run's single target. Every remote call goes through it.
        workspace: the run's scratch directory; holds the report and the trace.
        telethon_env: QA account credentials, present only when the deployment
            has a bot to talk to. No executor ever sees them.
        probe_runner: override for the Telegram child process, for tests.
        jobs: the run's scheduled-behaviour capability, present only when this
            run's criteria named a behaviour and the deployment holds the
            generated jobs capability. Like the Telegram credentials, no
            executor ever sees the capability itself.
        jobs_client_factory: override for the product jobs client, for tests.
    """
    capabilities = session.capabilities

    def record(tool: str, request: str, response: str) -> None:
        workspace.record(tool, request, response)

    def refuse(tool: str, request: str, error: QATargetError) -> dict:
        record(tool, request, f"refused: {error}")
        logger.info("qa_tool_refused", tool=tool, error=str(error))
        return {"error": str(error)}

    def write_qa_report(markdown: str) -> str:
        workspace.write_report(markdown)
        return f"QA report stored ({len(markdown)} characters)."

    callables: dict[str, Callable] = dict(_remote_tools(session, record, refuse))
    callables["write_qa_report"] = write_qa_report
    if jobs is not None and jobs.behaviours:
        jobs_capability = _JobsCapability(
            jobs=jobs, workspace=workspace, client_factory=jobs_client_factory
        )
        callables["fire_job"] = jobs_capability.fire_job
        callables["job_evidence"] = jobs_capability.job_evidence
    if capabilities.bot_username:
        if not telethon_env:
            raise ValueError("a bot target needs the QA account's Telethon credentials")
        telegram = _TelegramCapability(
            bot_username=capabilities.bot_username,
            workspace=workspace,
            telethon_env=telethon_env,
            probe_runner=probe_runner or run_probe_script,
        )
        callables["telegram_probe"] = telegram.telegram_probe
        callables["telegram_click_button"] = telegram.telegram_click_button
    return callables


def build_qa_tools(
    *,
    session: QATargetSession,
    workspace: QAWorkspace,
    telethon_env: dict[str, str] | None = None,
    probe_runner: Callable[..., object] | None = None,
    jobs: QAJobsCapability | None = None,
    jobs_client_factory: Callable[[str], GeneratedServiceJobsClient] | None = None,
) -> list[StructuredTool]:
    """Wrap this run's callables as LangChain tools for the in-process agent."""
    callables = build_qa_callables(
        session=session,
        workspace=workspace,
        telethon_env=telethon_env,
        probe_runner=probe_runner,
        jobs=jobs,
        jobs_client_factory=jobs_client_factory,
    )
    capabilities = session.capabilities
    remote = {name: fn for name, fn in callables.items() if name in _descriptions(capabilities)}
    tools = _describe(capabilities, remote, callables["write_qa_report"])
    if "fire_job" in callables:
        names = ", ".join(jobs.names) if jobs else ""
        tools.append(
            StructuredTool.from_function(
                coroutine=callables["fire_job"],
                name="fire_job",
                description=(
                    "Invoke one scheduled behaviour of the product by name, on the deployment "
                    f"under test. This run may fire: {names}. You supply only the name, and only "
                    "one of those — the arguments and the command identity belong to the run, "
                    "so calling it twice re-reads the same execution instead of causing a "
                    "second one. A successful answer records that the product's core dispatched "
                    "the fire; it is not evidence the behaviour ran. Assert that against the "
                    "product's own output."
                ),
            )
        )
        tools.append(
            StructuredTool.from_function(
                coroutine=callables["job_evidence"],
                name="job_evidence",
                description=(
                    "Read back what the product recorded for this run's fire of a named "
                    f"behaviour: {names}. Same rule — the record is dispatch evidence, not "
                    "proof that the behaviour happened."
                ),
            )
        )
    if "telegram_probe" in callables:
        tools.append(
            StructuredTool.from_function(
                coroutine=callables["telegram_probe"],
                name="telegram_probe",
                description=(
                    f"Send a message to @{capabilities.bot_username} as the platform's QA "
                    "Telegram account and return the bot's replies. This is the only way to talk "
                    "to the bot; you never hold the account's credentials."
                ),
            )
        )
        tools.append(
            StructuredTool.from_function(
                coroutine=callables["telegram_click_button"],
                name="telegram_click_button",
                description=(
                    "Invoke one inline button returned by telegram_probe in this QA run. "
                    "Use the reply id and callback_data exactly as returned; arbitrary callbacks "
                    "and callbacks from another bot are refused."
                ),
            )
        )
    return tools


def _descriptions(capabilities: QACapabilities) -> dict[str, str]:
    """What each tool promises the agent, stated as the capability that bounds it."""
    containers = ", ".join(sorted(capabilities.containers)) or "(none running)"
    ports = ", ".join(str(port) for port in sorted(capabilities.loopback_ports)) or "(none)"
    return {
        "http_get": (
            "GET a path on the deployed application over its public URL "
            f"({capabilities.deployed_url}). Returns status, headers and body. There is no way "
            "to send POST, PUT, PATCH or DELETE — QA never writes to the application."
        ),
        "localhost_http_get": (
            "GET a path on the target's loopback interface, for a service that is not "
            f"published publicly. Only ports allocated to this deployment work: {ports}. "
            "GET only, like http_get."
        ),
        "remote_read": (
            "Read a file from this deployment's directory on the target "
            f"({capabilities.physical_root}). The path is resolved on the target, so a symlink "
            "leading out of the deployment is refused, as are files holding deployment "
            "credentials."
        ),
        "remote_exec": (
            "Run one read-only docker command against a container of this deployment, as an "
            'argument vector, e.g. ["docker", "top", "<container>"]. Sub-commands: diff, '
            "inspect, logs, port, stats, top. There is no shell and no command that describes "
            f"the host. Containers you can name: {containers}."
        ),
        "container_logs": f"Tail the log of one of this deployment's containers: {containers}.",
        "container_inspect": (
            "Inspect the state of one of this deployment's containers: running, health, "
            f"exit code, restart count. Containers: {containers}."
        ),
    }


def _describe(
    capabilities: QACapabilities, remote_tools: dict, write_report
) -> list[StructuredTool]:
    """Bind the run's closures to their names and descriptions."""
    descriptions = _descriptions(capabilities)
    tools = [
        StructuredTool.from_function(coroutine=fn, name=name, description=descriptions[name])
        for name, fn in remote_tools.items()
    ]
    tools.append(
        StructuredTool.from_function(
            func=write_report,
            name="write_qa_report",
            description=(
                "Store the QA report in Markdown. Call this once, before returning the final "
                "JSON result."
            ),
        )
    )
    return tools

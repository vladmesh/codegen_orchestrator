"""The complete reach of a central QA run.

Every call here is built for one run and closes over that run's target session,
workspace and Telegram credentials. There is no module-level state and no
parameter naming a host, so an agent cannot address a second deployment: the
only target it can reach is the one the runner bound these calls to.

None of these can write to the application. The HTTP calls take no method, the
remote calls take an allowlist-checked argument vector, and the Telegram call
sends a message the platform's own test account is entitled to send.

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

import httpx
from langchain_core.tools import StructuredTool
import structlog

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


def build_qa_callables(
    *,
    session: QATargetSession,
    workspace: QAWorkspace,
    telethon_env: dict[str, str] | None = None,
    probe_runner: Callable[..., object] | None = None,
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
) -> list[StructuredTool]:
    """Wrap this run's callables as LangChain tools for the in-process agent."""
    callables = build_qa_callables(
        session=session,
        workspace=workspace,
        telethon_env=telethon_env,
        probe_runner=probe_runner,
    )
    capabilities = session.capabilities
    remote = {name: fn for name, fn in callables.items() if name in _descriptions(capabilities)}
    tools = _describe(capabilities, remote, callables["write_qa_report"])
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

"""The complete reach of a central QA run.

Every tool here is built for one run and closes over that run's target session,
workspace and Telegram credentials. There is no module-level state and no
parameter naming a host, so an agent cannot address a second deployment: the
only target it can reach is the one the runner bound these tools to.

None of these can write to the application. The HTTP tools take no method, the
remote tools take an allowlist-checked argument vector, and the Telegram tool
sends a message the platform's own test account is entitled to send. That is
the write guard — the old one filtered a shell the agent no longer has.

A refused call comes back as an ``error`` field rather than an exception: the
agent has to be able to read "that is out of scope" and choose another check,
and the runner keeps its own record of the refusal either way.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
from langchain_core.tools import StructuredTool
import structlog

from shared.telegram_access_probe import ProbeRun, run_probe_script
from shared.telegram_bot_probe import (
    TELEGRAM_REPLY_TIMEOUT,
    build_bot_message_script,
    parse_bot_replies,
)

from ...consumers._qa_target import QATarget, QATargetError, QATargetSession
from ...consumers._qa_workspace import QAWorkspace

logger = structlog.get_logger(__name__)

PUBLIC_PROBE_TIMEOUT = 30
MAX_BODY = 8000


def _truncate(text: str) -> str:
    return text[:MAX_BODY]


def _remote_tools(session: QATargetSession, record, refuse) -> dict:
    """The calls that leave the QA runtime, each scoped to one target."""
    target = session.target

    async def http_get(path: str) -> dict:
        url = f"{target.deployed_url.rstrip('/')}{path if path.startswith('/') else '/' + path}"
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


def build_qa_tools(
    *,
    session: QATargetSession,
    workspace: QAWorkspace,
    telethon_env: dict[str, str] | None = None,
    probe_runner: Callable[..., object] | None = None,
) -> list[StructuredTool]:
    """Build the tool set for exactly one QA run.

    Args:
        session: the run's single target. Every remote tool goes through it.
        workspace: the run's scratch directory; holds the report and the trace.
        telethon_env: QA account credentials, present only when the deployment
            has a bot to talk to. The agent never sees them.
        probe_runner: override for the Telegram child process, for tests.
    """
    target = session.target
    run_probe = probe_runner or run_probe_script

    def record(tool: str, request: str, response: str) -> None:
        workspace.record(tool, request, response)

    def refuse(tool: str, request: str, error: QATargetError) -> dict:
        record(tool, request, f"refused: {error}")
        logger.info("qa_tool_refused", tool=tool, error=str(error))
        return {"error": str(error)}

    async def telegram_probe(message: str) -> dict:
        request = f"@{target.bot_username} <- {message}"
        script = build_bot_message_script(target.bot_username, message)
        run: ProbeRun = await run_probe(
            script, env=telethon_env, timeout=TELEGRAM_REPLY_TIMEOUT + 30
        )
        if run.exit_status != 0:
            detail = (run.stderr or run.stdout or "probe failed").strip()[-1000:]
            record("telegram_probe", request, f"failed: {detail}")
            return {"error": detail}
        try:
            replies = parse_bot_replies(run.stdout)
        except ValueError as exc:
            record("telegram_probe", request, f"failed: {exc}")
            return {"error": str(exc)}
        record("telegram_probe", request, " | ".join(replies))
        return {"sent": message, "replies": replies}

    def write_qa_report(markdown: str) -> str:
        workspace.write_report(markdown)
        return f"QA report stored ({len(markdown)} characters)."

    tools = _describe(
        target,
        _remote_tools(session, record, refuse),
        write_qa_report,
    )
    if target.bot_username:
        if not telethon_env:
            raise ValueError("a bot target needs the QA account's Telethon credentials")
        tools.append(
            StructuredTool.from_function(
                coroutine=telegram_probe,
                name="telegram_probe",
                description=(
                    f"Send a message to @{target.bot_username} as the platform's QA Telegram "
                    "account and return the bot's replies. This is the only way to talk to the "
                    "bot; you never hold the account's credentials."
                ),
            )
        )
    return tools


def _descriptions(target: QATarget) -> dict[str, str]:
    """What each tool promises the agent, in the target's own terms."""
    return {
        "http_get": (
            "GET a path on the deployed application over its public URL "
            f"({target.deployed_url}). Returns status, headers and body. There is no way to "
            "send POST, PUT, PATCH or DELETE — QA never writes to the application."
        ),
        "localhost_http_get": (
            "GET a path on the target's loopback interface, for a service that is not "
            "published publicly. Arguments: port (1-65535) and path starting with '/'. "
            "GET only, like http_get."
        ),
        "remote_read": (
            "Read a file from the deployment directory on the target "
            f"({target.service_dir}). Paths outside it, and files holding deployment "
            "credentials, are refused."
        ),
        "remote_exec": (
            "Run one read-only command on the target as an argument vector, "
            'e.g. ["docker", "ps", "-a"]. There is no shell: no pipes, no redirection, no '
            "globbing. Only read-only programs are accepted and anything that could change "
            "the deployment is refused."
        ),
        "container_logs": (
            "Tail the log of one container belonging to this deployment "
            f"(names start with '{target.project_name}-')."
        ),
        "container_inspect": (
            "Inspect the state of one container of this deployment: running, health, "
            "exit code, restart count."
        ),
    }


def _describe(target: QATarget, remote_tools: dict, write_report) -> list[StructuredTool]:
    """Bind the run's closures to their names and descriptions."""
    descriptions = _descriptions(target)
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

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
    capabilities = session.capabilities
    run_probe = probe_runner or run_probe_script

    def record(tool: str, request: str, response: str) -> None:
        workspace.record(tool, request, response)

    def refuse(tool: str, request: str, error: QATargetError) -> dict:
        record(tool, request, f"refused: {error}")
        logger.info("qa_tool_refused", tool=tool, error=str(error))
        return {"error": str(error)}

    async def telegram_probe(message: str) -> dict:
        request = f"@{capabilities.bot_username} <- {message}"
        script = build_bot_message_script(capabilities.bot_username, message)
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
        session.capabilities,
        _remote_tools(session, record, refuse),
        write_qa_report,
    )
    if capabilities.bot_username:
        if not telethon_env:
            raise ValueError("a bot target needs the QA account's Telethon credentials")
        tools.append(
            StructuredTool.from_function(
                coroutine=telegram_probe,
                name="telegram_probe",
                description=(
                    f"Send a message to @{capabilities.bot_username} as the platform's QA "
                    "Telegram account and return the bot's replies. This is the only way to talk "
                    "to the bot; you never hold the account's credentials."
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

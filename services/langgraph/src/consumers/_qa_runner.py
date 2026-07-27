"""QA runners — HTTP checks for criteria we can decide, Claude Code for the rest.

Criteria that only state GET expectations are run directly against the deployed
URL by `run_health_checks`. Anything else goes to `run_qa_on_server`, which
delegates testing to the Claude Code CLI on the target server, prompted with the
acceptance criteria and deployment URL.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import re
import shlex
import time
import uuid

import asyncssh
import httpx
import structlog

from shared.contracts.acceptance import HealthCriterion
from shared.contracts.dto.run_result import (
    QABlocker,
    QABlockerCategory,
)

from ..prompts.qa import QA_TEST_TELEGRAM_ID, TELETHON_ENV_FILE, build_qa_prompt

logger = structlog.get_logger(__name__)

QA_TIMEOUT = 1200  # 20 minutes
HEALTH_CHECK_TIMEOUT = 30
HEALTH_CHECK_ATTEMPTS = 5
HEALTH_CHECK_RETRY_DELAY = 5
SERVICE_BASE_DIR = "/opt/services"
CREDENTIALS_PATH = "$HOME/.claude/.credentials.json"
LOCAL_CREDENTIALS_PATH = "/secrets/claude-credentials.json"  # mounted from host
OAUTH_ENDPOINT = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_REFRESH_BUFFER_S = 300  # refresh if expires within 5 minutes
CREDENTIAL_REFRESH_INTERVAL = 4 * 3600  # 4 hours
TELETHON_ENV_VARS = ("TELETHON_API_ID", "TELETHON_API_HASH", "TELETHON_SESSION")
# Sourced into the QA command itself, so the agent gets TELETHON_* whether or not
# it follows the prompt. Same reason PATH is exported here.
TELETHON_ENV_PREFIX = f"set -a && . {TELETHON_ENV_FILE} && set +a && "
CLAUDE_PATH_PREFIX = 'export PATH="$HOME/.local/bin:$PATH" && '
TELEGRAM_ACCESS_PROBE_TIMEOUT = 10
_WRITE_METHODS = "POST|PUT|PATCH|DELETE"


class TelethonCredentialsError(RuntimeError):
    """The QA server has no usable Telethon credentials for a bot run."""


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


def _qa_write_guard_settings(*, deployed_url: str, trace_path: str) -> str:
    """Build the Claude hook configuration that guards application API writes."""
    hook_command = (
        "/opt/qa-runner/qa-write-guard.py "
        f"--target {shlex.quote(deployed_url)} --trace {shlex.quote(trace_path)}"
    )
    return json.dumps(
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": hook_command}],
                    }
                ]
            }
        }
    )


async def _write_qa_write_guard_settings(
    conn: asyncssh.SSHClientConnection, *, deployed_url: str, trace_path: str
) -> str:
    """Install one-run Claude hook settings before exposing Bash to the agent."""
    settings_path = f"/tmp/qa-write-guard-{uuid.uuid4().hex}.json"  # noqa: S108
    payload = _qa_write_guard_settings(deployed_url=deployed_url, trace_path=trace_path)
    await conn.run(
        f"umask 077; printf %s {shlex.quote(payload)} > {shlex.quote(settings_path)}", check=True
    )
    return settings_path


async def _collect_qa_write_guard_trace(conn: asyncssh.SSHClientConnection, trace_path: str) -> str:
    """Return runner-owned write attempts recorded by the Claude Bash hook."""
    result = await conn.run(f"cat {shlex.quote(trace_path)} 2>/dev/null", check=False)
    if result.exit_status != 0:
        return ""
    for line in result.stdout.splitlines():
        if re.fullmatch(rf"(?:{_WRITE_METHODS})\s+\S+", line, flags=re.IGNORECASE):
            return line
    return ""


def _invalid_qa_payload(raw: str, reason: str) -> QAResult:
    """Fail closed when the agent's result cannot safely drive QA routing."""
    return QAResult(
        passed=False,
        summary=f"QA output has an invalid result shape: {reason}",
        raw=raw,
        blocker=_unknown_result_blocker(
            attempted="validate QA agent result",
            sent="Claude Code stdout",
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
    """Parse Claude Code's JSON output into a QAResult.

    Handles:
    - --output-format json wrapper: {"type":"result","result":"..."}
    - Raw QA JSON: {"pass": true, ...}
    - JSON wrapped in markdown code blocks
    """
    if not raw or not raw.strip():
        return QAResult(
            passed=False,
            summary="QA produced no output",
            raw=raw,
            blocker=_unknown_result_blocker(
                attempted="parse QA agent result",
                sent="Claude Code stdout",
                received="empty output",
            ),
        )

    json_str = raw.strip()

    # Step 1: Unwrap --output-format json wrapper if present
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
                sent="Claude Code stdout",
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
                sent="Claude Code stdout",
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


async def _ensure_claude_credentials(conn: asyncssh.SSHClientConnection) -> None:
    """Check Claude Code OAuth credentials on server, refresh if expired.

    Strategy:
    1. Read credentials from server
    2. If still valid — return
    3. Try OAuth refresh_token grant
    4. If refresh fails (400/401 = token revoked/expired) — fallback to local credentials
    """
    result = await conn.run(f"cat {CREDENTIALS_PATH} 2>/dev/null", check=False)
    if result.exit_status != 0 or not result.stdout:
        # No credentials on server at all — try pushing local
        logger.warning("claude_credentials_missing_on_server")
        await _push_local_credentials(conn)
        return

    creds = json.loads(result.stdout)
    oauth = creds["claudeAiOauth"]
    expires_at = oauth["expiresAt"] / 1000  # ms → seconds
    now = time.time()

    if now < expires_at - OAUTH_REFRESH_BUFFER_S:
        logger.info("claude_credentials_valid", ttl_s=int(expires_at - now))
        return

    logger.info("claude_credentials_expired", expired_ago_s=int(now - expires_at))

    # Try OAuth refresh
    try:
        await _refresh_oauth_token(conn, oauth)
        return
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (400, 401):
            logger.warning(
                "claude_refresh_token_invalid",
                status=e.response.status_code,
                body=e.response.text[:200],
            )
            # Refresh token is dead — fallback to local credentials
            await _push_local_credentials(conn)
        else:
            raise


async def _refresh_oauth_token(
    conn: asyncssh.SSHClientConnection,
    oauth: dict,
) -> None:
    """Refresh OAuth token and write updated credentials to server."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            OAUTH_ENDPOINT,
            data={
                "grant_type": "refresh_token",
                "refresh_token": oauth["refreshToken"],
                "client_id": OAUTH_CLIENT_ID,
            },
        )
        resp.raise_for_status()
        token_data = resp.json()

    now = time.time()
    new_creds = {
        "claudeAiOauth": {
            "accessToken": token_data["access_token"],
            "refreshToken": token_data["refresh_token"],
            "expiresAt": int((now + token_data["expires_in"]) * 1000),
            "scopes": oauth["scopes"],
            "subscriptionType": oauth.get("subscriptionType", ""),
            "rateLimitTier": oauth.get("rateLimitTier", ""),
        }
    }
    await _write_credentials(conn, new_creds)
    logger.info("claude_credentials_refreshed", expires_in=token_data["expires_in"])


async def _push_local_credentials(conn: asyncssh.SSHClientConnection) -> None:
    """Push local credentials file to server as fallback.

    Reads from LOCAL_CREDENTIALS_PATH (mounted from host) and writes to server.
    """
    try:
        with open(LOCAL_CREDENTIALS_PATH) as f:
            local_creds = json.load(f)
    except FileNotFoundError as err:
        raise RuntimeError(
            f"Refresh token expired and no local credentials at {LOCAL_CREDENTIALS_PATH}. "
            "Mount ~/.claude/.credentials.json into the container."
        ) from err

    local_oauth = local_creds["claudeAiOauth"]
    local_expires = local_oauth["expiresAt"] / 1000
    now = time.time()

    if now >= local_expires:
        raise RuntimeError(
            f"Local credentials are also expired "
            f"(expired {int(now - local_expires)}s ago). "
            "Run 'claude login' on the host machine."
        )

    await _write_credentials(conn, local_creds)
    logger.info(
        "claude_credentials_pushed_from_local",
        ttl_s=int(local_expires - now),
    )


async def _write_credentials(
    conn: asyncssh.SSHClientConnection,
    creds: dict,
) -> None:
    """Write credentials JSON to server."""
    creds_json = json.dumps(creds, indent=2)
    await conn.run(
        f"mkdir -p $HOME/.claude && cat > {CREDENTIALS_PATH} "
        f"<< 'CREDS_EOF'\n{creds_json}\nCREDS_EOF",
        check=True,
    )


async def _require_telethon_credentials(conn: asyncssh.SSHClientConnection) -> None:
    """Fail the run when the server's Telethon credentials are missing or empty.

    Without this the QA agent starts anyway and reports the Telegram checks as
    blocked on a guessed cause. Only variable names are echoed back, never values.
    """
    check = (
        f'test -r {TELETHON_ENV_FILE} || {{ echo "missing"; exit 1; }}; '
        f"set -a && . {TELETHON_ENV_FILE} && set +a; "
        "empty=; "
        f"for v in {' '.join(TELETHON_ENV_VARS)}; do "
        'eval "value=\\$$v"; [ -n "$value" ] || empty="$empty $v"; done; '
        '[ -z "$empty" ] || { echo "empty:$empty"; exit 1; }'
    )
    result = await conn.run(check, check=False)
    if result.exit_status == 0:
        return

    detail = (result.stdout or "").strip() or f"check failed with status {result.exit_status}"
    if detail == "missing":
        raise TelethonCredentialsError(f"no credentials file at {TELETHON_ENV_FILE} on the server")
    if detail.startswith("empty:"):
        raise TelethonCredentialsError(
            f"{TELETHON_ENV_FILE} has empty {detail.removeprefix('empty:').strip()}"
        )
    raise TelethonCredentialsError(f"cannot read {TELETHON_ENV_FILE}: {detail}")


async def _preflight_agent_qa(
    conn: asyncssh.SSHClientConnection, bot_username: str | None
) -> QABlocker | None:
    """Check platform-owned prerequisites without invoking Claude Code."""
    claude = await conn.run(f"{CLAUDE_PATH_PREFIX}command -v claude", check=False)
    if claude.exit_status != 0:
        return QABlocker(
            category=QABlockerCategory.CLAUDE_UNAVAILABLE,
            attempted="locate Claude Code on QA server",
            sent=f"{CLAUDE_PATH_PREFIX}command -v claude",
            received=(claude.stderr or claude.stdout or "command not found").strip(),
        )

    if not bot_username:
        return None

    try:
        await _require_telethon_credentials(conn)
    except TelethonCredentialsError as exc:
        return QABlocker(
            category=QABlockerCategory.MISSING_TELETHON_CREDENTIALS,
            attempted="validate QA Telethon credentials",
            sent=f"read {TELETHON_ENV_FILE} and validate required variable names",
            received=str(exc),
        )

    probe = (
        f"set -a && . {TELETHON_ENV_FILE} && set +a; "
        "/opt/qa-runner/venv/bin/python3 -c "
        + shlex.quote(
            "import os\n"
            "from telethon.sync import TelegramClient\n"
            "from telethon.sessions import StringSession\n"
            "import sys\n"
            "import time\n"
            "client = TelegramClient(StringSession(os.environ['TELETHON_SESSION']), "
            "int(os.environ['TELETHON_API_ID']), os.environ['TELETHON_API_HASH'])\n"
            "client.start()\n"
            "try:\n"
            "    me = client.get_me()\n"
            f"    if me.id != {QA_TEST_TELEGRAM_ID}:\n"
            "        print('telegram_identity_mismatch:expected="
            f"{QA_TEST_TELEGRAM_ID};actual=' + str(me.id))\n"
            "        sys.exit(3)\n"
            f"    bot = client.get_entity('@{bot_username}')\n"
            "    sent = client.send_message(bot, '/start')\n"
            f"    deadline = time.monotonic() + {TELEGRAM_ACCESS_PROBE_TIMEOUT}\n"
            "    while time.monotonic() < deadline:\n"
            "        replies = client.get_messages(bot, min_id=sent.id, limit=5)\n"
            "        for reply in replies:\n"
            "            if reply.out or reply.id <= sent.id:\n"
            "                continue\n"
            "            text = (reply.raw_text or reply.message or '').strip()\n"
            "            normalized = text.casefold()\n"
            "            denied = ('доступ запрещ' in normalized or 'access denied' in normalized "
            "or 'not authorized' in normalized or 'unauthorized' in normalized "
            "or 'forbidden' in normalized)\n"
            "            if denied:\n"
            "                print('telegram_access_denied:' + text[:500])\n"
            "                sys.exit(2)\n"
            "        time.sleep(1)\n"
            "    print('telegram_access_probe_passed')\n"
            "finally:\n"
            "    client.disconnect()"
        )
    )
    result = await conn.run(probe, check=False)
    if result.exit_status != 0:
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        denial_marker = "telegram_access_denied:"
        identity_marker = "telegram_identity_mismatch:"
        if identity_marker in stdout:
            category = QABlockerCategory.UNKNOWN
            received = stdout.split(identity_marker, maxsplit=1)[1].strip()[-500:]
        elif denial_marker in stdout:
            category = QABlockerCategory.TELEGRAM_ACCESS_DENIED
            received = stdout.split(denial_marker, maxsplit=1)[1].strip()[-500:]
        else:
            category = QABlockerCategory.UNKNOWN
            received = stderr or stdout or "Telegram probe failed"
        return QABlocker(
            category=category,
            attempted="verify the deterministic Telegram QA identity and send /start access probe",
            sent=f"Telegram /start to @{bot_username}",
            received=received,
        )
    return None


async def run_qa_on_server(
    *,
    server_ip: str,
    ssh_user: str,
    ssh_key: str,
    project_name: str,
    acceptance_criteria: str,
    deployed_url: str,
    bot_username: str | None = None,
    timeout: int = QA_TIMEOUT,
) -> QAResult:
    """SSH to server, run Claude Code with QA prompt, return parsed result.

    Args:
        server_ip: Target server IP address
        ssh_key: PEM-encoded SSH private key
        project_name: Project directory name under /opt/services/
        acceptance_criteria: Regression test criteria from repository
        deployed_url: URL where the project is deployed
        bot_username: Telegram bot username (if applicable)
        timeout: Timeout in seconds for the Claude Code run
    Returns:
        QAResult with pass/fail status and check details
    """
    prompt = build_qa_prompt(acceptance_criteria, deployed_url, bot_username)
    guard_trace_path = f"/tmp/qa-write-guard-{uuid.uuid4().hex}.jsonl"  # noqa: S108

    # Escape prompt for shell — use heredoc to avoid quoting issues
    # Prepend ~/.local/bin to PATH — non-interactive SSH doesn't source .bashrc
    # Permissions are configured via ~/.claude/settings.json (allowlist).
    cmd = CLAUDE_PATH_PREFIX + (
        f"{TELETHON_ENV_PREFIX if bot_username else ''}"
        f"cd {shlex.quote(f'{SERVICE_BASE_DIR}/{project_name}')} && "
        f"timeout {timeout} claude -p {shlex.quote(prompt)} "
        f"--output-format json "
        f"--max-turns 200 "
        f"--model claude-sonnet-4-6 "
        f"2>/dev/null"
    )

    qa_result: QAResult | None = None
    try:
        key = asyncssh.import_private_key(ssh_key)
        async with asyncssh.connect(
            server_ip,
            username=ssh_user,
            known_hosts=None,
            client_keys=[key],
        ) as conn:
            logger.info(
                "qa_ssh_connected",
                server_ip=server_ip,
                project_name=project_name,
                timeout=timeout,
            )

            # Ensure Claude Code credentials are fresh before running
            blocker = await _preflight_agent_qa(conn, bot_username)
            if blocker:
                return QAResult(passed=False, summary="QA preflight blocked", blocker=blocker)

            await _ensure_claude_credentials(conn)
            guard_settings_path = await _write_qa_write_guard_settings(
                conn, deployed_url=deployed_url, trace_path=guard_trace_path
            )

            result = await conn.run(
                f"{cmd} --settings {shlex.quote(guard_settings_path)}", check=False
            )

            # Collect QA_REPORT.md regardless of exit status
            report = await _collect_qa_report(conn, project_name)
            guarded_write = await _collect_qa_write_guard_trace(conn, guard_trace_path)
            await conn.run(
                f"rm -f {shlex.quote(guard_settings_path)} {shlex.quote(guard_trace_path)}",
                check=False,
            )

            if guarded_write:
                qa_result = QAResult(
                    passed=False,
                    report=report,
                    raw=result.stdout or "",
                    summary="QA attempted a forbidden application API write",
                )
                return _block_forbidden_application_write(qa_result, guarded_write)

            if result.exit_status != 0:
                logger.warning(
                    "qa_claude_nonzero_exit",
                    server_ip=server_ip,
                    exit_status=result.exit_status,
                    stderr=result.stderr[:500] if result.stderr else "",
                )
                if result.stdout:
                    qa_result = parse_qa_result(result.stdout)
                    qa_result.report = report
                    if qa_result.blocker:
                        qa_result.blocker = _unknown_result_blocker(
                            attempted="run Claude Code QA command",
                            sent=cmd,
                            received=(
                                f"exit_status={result.exit_status}; "
                                f"stdout={result.stdout[:2000]}; "
                                f"stderr={(result.stderr or '')[:2000]}"
                            ),
                        )
                    write = _forbidden_application_write(f"{result.stdout}\n{report}", deployed_url)
                    return (
                        _block_forbidden_application_write(qa_result, write) if write else qa_result
                    )
                qa_result = QAResult(
                    passed=False,
                    summary=f"Claude Code exited with status {result.exit_status}: "
                    f"{result.stderr[:300] if result.stderr else 'no output'}",
                    raw=result.stdout or "",
                    report=report,
                    blocker=_unknown_result_blocker(
                        attempted="run Claude Code QA command",
                        sent=cmd,
                        received=(
                            f"exit_status={result.exit_status}; stdout=; "
                            f"stderr={(result.stderr or '')[:2000]}"
                        ),
                    ),
                )
                write = _forbidden_application_write(f"{result.stdout}\n{report}", deployed_url)
                return _block_forbidden_application_write(qa_result, write) if write else qa_result

            qa_result = parse_qa_result(result.stdout or "")
            qa_result.report = report
            write = _forbidden_application_write(f"{result.stdout}\n{report}", deployed_url)
            return _block_forbidden_application_write(qa_result, write) if write else qa_result

    except TelethonCredentialsError as e:
        logger.error("qa_telethon_credentials_unusable", server_ip=server_ip, error=str(e))
        qa_result = QAResult(
            passed=False,
            summary=f"QA cannot test @{bot_username}: {e}",
            raw="",
            blocker=QABlocker(
                category=QABlockerCategory.MISSING_TELETHON_CREDENTIALS,
                attempted="validate QA Telethon credentials",
                sent=f"read {TELETHON_ENV_FILE}",
                received=str(e),
            ),
        )
        return qa_result

    except Exception as e:
        logger.error("qa_ssh_failed", server_ip=server_ip, error=str(e))
        qa_result = QAResult(
            passed=False,
            summary=f"SSH connection failed to {server_ip}: {e}",
            raw="",
            blocker=QABlocker(
                category=QABlockerCategory.SERVER_UNAVAILABLE,
                attempted="connect to QA server",
                sent=f"SSH connection to {server_ip}",
                received=str(e),
            ),
        )
        return qa_result


async def _collect_qa_report(
    conn: asyncssh.SSHClientConnection,
    project_name: str,
) -> str:
    """Read and remove QA_REPORT.md from the project directory on the server."""
    report_path = f"{SERVICE_BASE_DIR}/{project_name}/QA_REPORT.md"
    quoted_report_path = shlex.quote(report_path)
    try:
        result = await conn.run(f"cat {quoted_report_path} 2>/dev/null", check=False)
        if result.exit_status == 0 and result.stdout:
            await conn.run(f"rm -f {quoted_report_path}", check=False)
            logger.info("qa_report_collected", size=len(result.stdout))
            return result.stdout
    except Exception as e:
        logger.warning("qa_report_collect_failed", error=str(e))
    return ""


async def credential_refresh_loop() -> None:
    """Periodically refresh Claude Code credentials on all managed servers.

    Runs every CREDENTIAL_REFRESH_INTERVAL (4h). Connects to each server
    via SSH and calls _ensure_claude_credentials to keep tokens fresh.
    This prevents refresh tokens from expiring between QA runs.
    """
    from ..clients.api import api_client

    logger.info("credential_refresh_loop_started", interval_s=CREDENTIAL_REFRESH_INTERVAL)

    while True:
        try:
            servers = await api_client.list_servers(is_managed=True)
            for server in servers:
                if not server.public_ip:
                    continue
                ssh_key = await api_client.get_server_ssh_key(server.handle)
                if not ssh_key:
                    continue
                try:
                    key = asyncssh.import_private_key(ssh_key)
                    async with asyncssh.connect(
                        server.public_ip,
                        username=server.ssh_user,
                        known_hosts=None,
                        client_keys=[key],
                    ) as conn:
                        await _ensure_claude_credentials(conn)
                        logger.info(
                            "credential_refresh_ok",
                            server_ip=server.public_ip,
                        )
                except Exception:
                    logger.exception(
                        "credential_refresh_server_error",
                        server_ip=server.public_ip,
                    )
        except Exception:
            logger.exception("credential_refresh_cycle_error")

        await asyncio.sleep(CREDENTIAL_REFRESH_INTERVAL)

"""QA runner — SSH to prod server, run Claude Code, parse result.

Delegates actual testing to Claude Code CLI running on the target server.
The prompt is built from the story description and deployment URL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re

import asyncssh
import structlog

logger = structlog.get_logger(__name__)

QA_TIMEOUT = 1200  # 20 minutes
SERVICE_BASE_DIR = "/opt/services"


@dataclass
class QAResult:
    """Structured result from a QA run."""

    passed: bool
    checks: list[dict] = field(default_factory=list)
    summary: str = ""
    raw: str = ""


def build_qa_prompt(
    story_description: str,
    deployed_url: str,
    bot_username: str | None = None,
) -> str:
    """Build the QA prompt for Claude Code on the server."""
    bot_section = ""
    if bot_username:
        bot_section = f"""- Bot: @{bot_username}

For Telegram bot testing, Telethon is pre-installed.
Session file: /opt/qa-runner/telethon.session
Use: python3 -c "from telethon.sync import TelegramClient; ..."
"""

    return f"""\
You are a QA tester. Test this deployed project as a real user would.

## Story (what the user asked for)
{story_description}

## Deployed at
- URL: {deployed_url}
{bot_section}
## Your task
1. Test every feature described in the story
2. For web/API: curl endpoints, check responses, test edge cases
3. For Telegram bots: use Telethon to send commands, check responses
4. Check that the UI/responses match the story description

## Rules
- Test ONLY what's described in the story — don't invent extra requirements
- Be practical: if the story says "show weather", check that weather is shown, not pixel-perfect
- Timeout: 20 minutes max

## Output
Return ONLY a JSON object:
{{
  "pass": true/false,
  "checks": [{{"name": "check name", "pass": true/false, "detail": "what happened"}}],
  "summary": "brief summary"
}}"""


def parse_qa_result(raw: str) -> QAResult:
    """Parse Claude Code's JSON output into a QAResult.

    Handles raw JSON, JSON wrapped in markdown code blocks, and malformed output.
    """
    if not raw or not raw.strip():
        return QAResult(passed=False, summary="QA produced no output", raw=raw)

    # Try to extract JSON from markdown code blocks
    json_str = raw.strip()
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
        )

    passed = data.get("pass", False)
    if not isinstance(passed, bool):
        passed = False

    return QAResult(
        passed=passed,
        checks=data.get("checks", []),
        summary=data.get("summary", ""),
        raw=raw,
    )


async def run_qa_on_server(
    *,
    server_ip: str,
    ssh_key: str,
    project_name: str,
    story_description: str,
    deployed_url: str,
    bot_username: str | None = None,
    timeout: int = QA_TIMEOUT,
) -> QAResult:
    """SSH to server, run Claude Code with QA prompt, return parsed result.

    Args:
        server_ip: Target server IP address
        ssh_key: PEM-encoded SSH private key
        project_name: Project directory name under /opt/services/
        story_description: Full story description for the QA prompt
        deployed_url: URL where the project is deployed
        bot_username: Telegram bot username (if applicable)
        timeout: Timeout in seconds for the Claude Code run

    Returns:
        QAResult with pass/fail status and check details
    """
    prompt = build_qa_prompt(story_description, deployed_url, bot_username)

    # Escape prompt for shell — use heredoc to avoid quoting issues
    cmd = (
        f"cd {SERVICE_BASE_DIR}/{project_name} && "
        f"timeout {timeout} claude -p {_shell_quote(prompt)} "
        f"--output-format json "
        f"--max-turns 50 "
        f"--model claude-sonnet-4-6 "
        f"2>/dev/null"
    )

    try:
        key = asyncssh.import_private_key(ssh_key)
        async with asyncssh.connect(
            server_ip,
            username="root",
            known_hosts=None,
            client_keys=[key],
        ) as conn:
            logger.info(
                "qa_ssh_connected",
                server_ip=server_ip,
                project_name=project_name,
                timeout=timeout,
            )
            result = await conn.run(cmd, check=False)

            if result.exit_status != 0:
                logger.warning(
                    "qa_claude_nonzero_exit",
                    server_ip=server_ip,
                    exit_status=result.exit_status,
                    stderr=result.stderr[:500] if result.stderr else "",
                )
                if result.stdout:
                    return parse_qa_result(result.stdout)
                return QAResult(
                    passed=False,
                    summary=f"Claude Code exited with status {result.exit_status}: "
                    f"{result.stderr[:300] if result.stderr else 'no output'}",
                    raw=result.stdout or "",
                )

            return parse_qa_result(result.stdout or "")

    except Exception as e:
        logger.error("qa_ssh_failed", server_ip=server_ip, error=str(e))
        return QAResult(
            passed=False,
            summary=f"SSH connection failed to {server_ip}: {e}",
            raw="",
        )


def _shell_quote(s: str) -> str:
    """Quote a string for safe use in shell commands using $'...' syntax."""
    return "'" + s.replace("'", "'\\''") + "'"

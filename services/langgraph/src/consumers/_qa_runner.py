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
    report: str = ""


def build_qa_prompt(
    acceptance_criteria: str,
    deployed_url: str,
    bot_username: str | None = None,
) -> str:
    """Build the QA prompt for Claude Code on the server.

    Args:
        acceptance_criteria: Full regression test criteria from the repository.
        deployed_url: URL where the application is deployed.
        bot_username: Telegram bot username (if applicable).
    """
    bot_section = ""
    if bot_username:
        bot_section = f"""
### Telegram bot
- Bot: @{bot_username}
- Test via Telethon (pre-installed in /opt/qa-runner/venv):
  ```bash
  /opt/qa-runner/venv/bin/python3 -c "
  from telethon.sync import TelegramClient
  client = TelegramClient('/opt/qa-runner/telethon.session', api_id=0, api_hash='')
  client.start()
  client.send_message('@{bot_username}', '/start')
  import time; time.sleep(3)
  msgs = client.get_messages('@{bot_username}', limit=3)
  for m in msgs:
      print(m.text)
  client.disconnect()
  "
  ```
- api_id/api_hash can be 0/empty when session file already exists
"""

    return f"""\
You are a QA tester doing REGRESSION testing of a deployed project.

Your job is to TEST THE RUNNING APPLICATION as a real user would — by making
HTTP requests, sending Telegram commands, and observing actual responses.
You must verify ALL acceptance criteria below — this is a regression test,
not just a check of the latest feature.

CRITICAL RULES:
- You are testing a DEPLOYED APPLICATION, not reviewing source code.
- Do NOT read source code, do NOT docker exec into containers, do NOT inspect
  implementation. You are a BLACK-BOX tester.
- Every check MUST be based on an actual request/response you performed.
- "Code inspection confirms X" is NOT a valid test result.
- If a test requires sending a Telegram command, you MUST actually send it
  and verify the bot's response — not read the handler code.

## Acceptance Criteria (what the application must do)
{acceptance_criteria}

## Deployment
- URL: {deployed_url}
- Compose (status only): see "Container health" below
{bot_section}
## How to test

### REST API — use curl:
```bash
curl -sf {deployed_url}/health | jq .
curl -sf {deployed_url}/api/<endpoint> | jq .
```

### Container health — check status only (no exec):
```bash
cd infra && docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml ps -a
```

## Checklist
1. Health endpoint responds with 200
2. Every check from acceptance criteria — execute and verify
3. Containers running and healthy (ps, no restart loops)
4. Edge cases — empty input, missing parameters, invalid values

## Report
Write QA_REPORT.md in the project root (NOT in infra/).
In each check, describe WHAT YOU DID and WHAT YOU RECEIVED — paste actual
curl output or bot response. Do not describe code.

```markdown
# QA Report

## Summary
- **Result**: passed / failed
- **Checks**: X passed, Y failed

## Checks

### 1. <check name>
- **Result**: pass / fail
- **Detail**: <exact command you ran and response you got>

## Issues Encountered
(any problems found, or "None")
```

## Output
After writing QA_REPORT.md, return ONLY this JSON:
{{
  "pass": true/false,
  "checks": [{{"name": "check name", "pass": true/false, "detail": "one-line summary"}}],
  "summary": "brief summary"
}}"""


def parse_qa_result(raw: str) -> QAResult:
    """Parse Claude Code's JSON output into a QAResult.

    Handles:
    - --output-format json wrapper: {"type":"result","result":"..."}
    - Raw QA JSON: {"pass": true, ...}
    - JSON wrapped in markdown code blocks
    """
    if not raw or not raw.strip():
        return QAResult(passed=False, summary="QA produced no output", raw=raw)

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

    # Escape prompt for shell — use heredoc to avoid quoting issues
    # Prepend ~/.local/bin to PATH — non-interactive SSH doesn't source .bashrc
    # Permissions are configured via /root/.claude/settings.json (allowlist),
    # NOT --dangerously-skip-permissions (blocked when running as root).
    cmd = (
        f'export PATH="$HOME/.local/bin:$PATH" && '
        f"cd {SERVICE_BASE_DIR}/{project_name} && "
        f"timeout {timeout} claude -p {_shell_quote(prompt)} "
        f"--output-format json "
        f"--max-turns 30 "
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

            # Collect QA_REPORT.md regardless of exit status
            report = await _collect_qa_report(conn, project_name)

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
                    return qa_result
                return QAResult(
                    passed=False,
                    summary=f"Claude Code exited with status {result.exit_status}: "
                    f"{result.stderr[:300] if result.stderr else 'no output'}",
                    raw=result.stdout or "",
                    report=report,
                )

            qa_result = parse_qa_result(result.stdout or "")
            qa_result.report = report
            return qa_result

    except Exception as e:
        logger.error("qa_ssh_failed", server_ip=server_ip, error=str(e))
        return QAResult(
            passed=False,
            summary=f"SSH connection failed to {server_ip}: {e}",
            raw="",
        )


async def _collect_qa_report(
    conn: asyncssh.SSHClientConnection,
    project_name: str,
) -> str:
    """Read and remove QA_REPORT.md from the project directory on the server."""
    report_path = f"{SERVICE_BASE_DIR}/{project_name}/QA_REPORT.md"
    try:
        result = await conn.run(f"cat {report_path} 2>/dev/null", check=False)
        if result.exit_status == 0 and result.stdout:
            await conn.run(f"rm -f {report_path}", check=False)
            logger.info("qa_report_collected", size=len(result.stdout))
            return result.stdout
    except Exception as e:
        logger.warning("qa_report_collect_failed", error=str(e))
    return ""


def _shell_quote(s: str) -> str:
    """Quote a string for safe use in shell commands using $'...' syntax."""
    return "'" + s.replace("'", "'\\''") + "'"

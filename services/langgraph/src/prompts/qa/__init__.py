"""QA tester prompt — black-box regression testing of a deployed project.

The QA tester is not an in-graph LLM node: it runs a standalone Claude Code CLI
on the target server (see ``consumers/_qa_runner.run_qa_on_server``). This module
holds the prompt that drives that run, kept here for consistency with the other
agent prompts (``architect``, ``po``, ``developer_worker``).
"""

# Written by the qa_runner Ansible role into the QA user's home
# (services/infra-service/ansible/roles/qa_runner). The runner sources it into
# the QA command's environment (consumers/_qa_runner), so the agent gets
# TELETHON_* without doing anything.
TELETHON_ENV_FILE = "$HOME/.qa-telethon.env"


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
- You write to the bot as a real Telegram user. TELETHON_API_ID,
  TELETHON_API_HASH and TELETHON_SESSION (an authorized StringSession) are
  already exported in your shell. Do not source anything, do not look for a
  session file, and never print the values or paste them into the report.
- Test via Telethon (pre-installed in /opt/qa-runner/venv). Run this verbatim —
  the python body must stay unindented or python3 -c raises IndentationError:

```bash
/opt/qa-runner/venv/bin/python3 -c "
import os, time
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
client = TelegramClient(
    StringSession(os.environ['TELETHON_SESSION']),
    int(os.environ['TELETHON_API_ID']),
    os.environ['TELETHON_API_HASH'],
)
client.start()
client.send_message('@{bot_username}', '/start')
time.sleep(3)
for m in client.get_messages('@{bot_username}', limit=3):
    print(m.text)
client.disconnect()
"
```
- Every Telegram check is either pass or fail, decided by running the snippet
  above. "Blocked", "skipped" and "cannot test" are not allowed results: if you
  have not run the snippet, you have no result to report. Do not substitute code
  reading, and do not fall back to a session file path.
- If the snippet errors, run it once more, then report the Telegram checks as
  failed and paste the traceback's last line as the detail.
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

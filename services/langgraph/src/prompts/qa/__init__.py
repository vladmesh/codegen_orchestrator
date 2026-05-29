"""QA tester prompt — black-box regression testing of a deployed project.

The QA tester is not an in-graph LLM node: it runs a standalone Claude Code CLI
on the target server (see ``consumers/_qa_runner.run_qa_on_server``). This module
holds the prompt that drives that run, kept here for consistency with the other
agent prompts (``architect``, ``po``, ``developer_worker``).
"""


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

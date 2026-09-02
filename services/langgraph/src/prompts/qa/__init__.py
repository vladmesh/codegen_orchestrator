"""QA tester prompt — black-box regression testing of a deployed project.

Exploratory QA is performed by a central ephemeral coding agent on the
management host (see ``clients/qa_worker`` and ``consumers/_qa_runner``), and by
an in-process ReactAgent when no subscription session is available and the
optional API triplet is (see ``agents/qa/graph``). The two reach the deployment
through the same closed set of calls — one over the run's capability endpoint,
one as LangChain tools — so the rules below are written once and only the
"how you call them" section differs.

The rules themselves are unchanged from the on-target Claude Code run they
replaced: test the running application, never read implementation for evidence,
never write to the application's data, report the same JSON. One call asks the
product to act rather than to answer — a *named* scheduled behaviour, named by
this run's own acceptance criteria — and the rule that comes with it is stated
here once: the product core's record of having dispatched that fire is not
evidence the behaviour ran, and a check never passes on it.

What a run may carry now is a short list of facts the QA runner established
deterministically before the executor started — container state, and whether the
bot answered `getMe`. They are stated as given and struck off the checklist, so
the run is not spent rediscovering them. The tools, the rules and the result JSON
are the same whether or not that list is there.
"""

from collections.abc import Sequence
from enum import StrEnum

from shared.contracts.bot_access import QA_TEST_TELEGRAM_ID
from shared.qa_probe_cli import QA_PROBE_NAME, QA_PROBE_USAGE

__all__ = [
    "QA_TEST_TELEGRAM_ID",
    "QAExecutorKind",
    "build_qa_instructions",
    "build_qa_prompt",
]


class QAExecutorKind(StrEnum):
    """Which executor is being prompted, and therefore how calls are spelled."""

    # A coding agent in its own container, calling the run's capability endpoint
    # through the injected `qa` command.
    CENTRAL_AGENT = "central_agent"
    # The in-process ReactAgent fallback, calling LangChain tools.
    IN_PROCESS_TOOLS = "in_process_tools"


_TOOL_SECTION = """\
## Your tools
- `http_get(path)` — request a path on the deployed URL. Returns status,
  headers and body.
- `localhost_http_get(port, path)` — the same, from inside the target, for a
  service that is not published publicly.
- `container_inspect(container)` — is the container running, healthy, restarting?
- `container_logs(container, tail)` — what the container logged.
- `remote_exec(command)` — one read-only command as an argument vector, e.g.
  `["docker", "ps", "-a"]`. No shell, no pipes, no redirection.
- `remote_read(path)` — read a file from the deployment directory.
- `fire_job(name)` — invoke one scheduled behaviour of the product by name.
  Only a name this run's criteria declared, and only the name: the arguments and
  the command identity are the run's. Firing the same name twice re-reads the
  same execution.
- `job_evidence(name)` — read back what the product recorded for this run's fire.
- `write_qa_report(markdown)` — store the report.
"""

_DISPATCH_RULE = """\
- A fired job answers with a dispatch record. `dispatch_status: dispatched`
  means the product's core published the event and nothing more — it is not
  evidence that anything consumed it, and not evidence that the behaviour ran.
  Never pass a check on it. The check passes on the product's own output: what
  it sent, wrote or now exposes, observed with the calls above.
"""

_PROBE_SECTION = f"""\
## Your tools

You have a shell, and it reaches nothing. This container holds no SSH key, no
server address, no Telegram session and no credential of any kind. The single
command below is your whole reach into the deployment; it posts a named call to
the QA runtime on the management host, which performs it against the one
deployment this run is bound to and prints the JSON answer.

```
{QA_PROBE_USAGE}
```

- Run `{QA_PROBE_NAME} capabilities` first. It tells you the deployment's public
  URL, the loopback ports you may reach, the containers you may name and the
  directory you may read. A call outside that set is refused, and a refusal is
  not a product failure — choose another check.
- None of these calls writes to the application's data. `{QA_PROBE_NAME} http_get`
  and `{QA_PROBE_NAME} localhost_http_get` take no method; `{QA_PROBE_NAME}
  remote_exec` takes read-only docker sub-commands against your own containers
  and nothing else.
- `{QA_PROBE_NAME} fire_job <name>` is the one call that asks the product to *do*
  something, and only a scheduled behaviour this run's criteria named. You pass
  the name and nothing else — the arguments and the command identity belong to
  this run, so firing the same name again re-reads the same single execution
  rather than causing a second one. `{QA_PROBE_NAME} job_evidence <name>` reads
  that record back.
- There is no other way to reach the application, so do not spend the run
  looking for one. This container is on a network with no route to the
  deployment, to the fleet or to the internet: curl, a script you write and any
  package you install all reach nothing. The runner also scans everything this
  run produced for a direct write and blocks the run when it finds one,
  whatever the verdict said.
- Your workspace is a scratch directory that is destroyed with this container.
  Nothing you write there is delivered anywhere; the report and the result are
  delivered by `{QA_PROBE_NAME} report` and `{QA_PROBE_NAME} finish`.
"""


def _bot_section(bot_username: str, executor: QAExecutorKind) -> str:
    message_call = (
        f"`{QA_PROBE_NAME} telegram_probe <message>`"
        if executor is QAExecutorKind.CENTRAL_AGENT
        else "the `telegram_probe` tool"
    )
    callback_call = (
        f"`{QA_PROBE_NAME} telegram_click_button <message id> <callback data>`"
        if executor is QAExecutorKind.CENTRAL_AGENT
        else "the `telegram_click_button` tool"
    )
    return f"""
### Telegram bot
- Bot: @{bot_username}
- Use {message_call}. It sends your message to the bot as the
  platform's QA Telegram account and returns structured reply evidence: text,
  caption, media type and keyboard buttons. A media-only reply is evidence.
- Invoke a visible inline button only with {callback_call}, using the reply id
  and callback data returned by the probe. It returns the callback answer and
  every resulting bot reply, plus post-press evidence for the clicked message
  so an edit-in-place is observable.
- You never hold the account's credentials, and there is no other way to reach Telegram.
- Every Telegram check is either pass or fail, decided by sending the message.
  "Blocked", "skipped" and "cannot test" are not allowed results: if you have not
  sent the message, you have no result to report. Do not substitute code reading.
- If either Telegram call returns an error, stop testing and submit no product
  failure for it. The runtime records this as a non-product blocker.
"""


def _established_section(facts: Sequence[str]) -> str:
    """What the runner already knows, told to the executor as given.

    These facts were read by the QA runner itself, deterministically, against
    this same deployment and moments before this prompt was built. Repeating
    them here is what makes the checklist below able to stop asking for them:
    the run is for the checks only an exploratory tester can make.
    """
    if not facts:
        return ""
    body = "\n".join(facts)
    return f"""
## Already established (checked by the QA runner, not by you)
{body}
Treat these as given. Do not re-check them, and do not report them as your own
checks — they are already in this run's result.
"""


def _container_checklist_item(facts: Sequence[str]) -> str:
    """Ask for container state only when nobody has established it yet."""
    if facts:
        return "Container state — already established above; do not check it again"
    return "Containers running and healthy (no restart loops)"


def _report_section(executor: QAExecutorKind) -> str:
    if executor is QAExecutorKind.CENTRAL_AGENT:
        return f"""\
## Report
Write the Markdown below to a file and store it with
`{QA_PROBE_NAME} report <file>`.\
"""
    return """\
## Report
Call `write_qa_report` with the Markdown below.\
"""


def _output_section(executor: QAExecutorKind) -> str:
    if executor is QAExecutorKind.CENTRAL_AGENT:
        return f"""\
## Output
After storing the report, write this JSON to a file and submit it with
`{QA_PROBE_NAME} finish <file>`. That call ends the run — make it exactly once,
and only after every check is done.
{_RESULT_JSON}
The run is judged from what `{QA_PROBE_NAME} finish` received. A run that never
calls it has no result, and is reported to a human as unverified rather than as
a passing or failing product.\
"""
    return f"""\
## Output
After calling `write_qa_report`, return ONLY this JSON as your final message:
{_RESULT_JSON}\
"""


_RESULT_JSON = """\
{
  "pass": true/false,
  "checks": [{"name": "check name", "pass": true/false, "detail": "one-line summary"}],
  "summary": "brief summary"
}
"""


def build_qa_instructions() -> str:
    """The static rules written into a central QA executor's instruction file.

    Kept apart from the run's prompt because it is what the container is built
    with, not what this run asks for: it says what kind of agent this is and
    what it must never do, and it is identical for every QA run.
    """
    return f"""\
# QA executor

You are the platform's QA tester. You are not a developer: there is no
repository in this container, nothing you write here is kept, and you must never
try to change the application you are testing.

- The task for this run is in `/workspace/TASK.md`.
- `{QA_PROBE_NAME}` is your only route to the deployment. Run
  `{QA_PROBE_NAME} help` to see the calls, and `{QA_PROBE_NAME} capabilities` to
  see what this run may reach.
- Never attempt to reach the application other than through `{QA_PROBE_NAME}`,
  and never attempt any request to it that is not a GET. The one thing you may
  ask the application to *do* is `{QA_PROBE_NAME} fire_job <name>`, for a
  scheduled behaviour this run's task names; what it answers with is a dispatch
  record, which is never on its own evidence that the behaviour happened.
- Finish by storing a report with `{QA_PROBE_NAME} report` and submitting the
  result JSON with `{QA_PROBE_NAME} finish`. Nothing else you do is delivered.
"""


def build_qa_prompt(
    acceptance_criteria: str,
    deployed_url: str,
    bot_username: str | None = None,
    *,
    executor: QAExecutorKind = QAExecutorKind.IN_PROCESS_TOOLS,
    established_facts: Sequence[str] = (),
) -> str:
    """Build the QA prompt for the executor that will carry out this run.

    Args:
        acceptance_criteria: Full regression test criteria from the repository.
        deployed_url: URL where the application is deployed.
        bot_username: Telegram bot username (if applicable).
        executor: which executor is being prompted. It changes only how the
            calls are spelled, never what they are or what is allowed.
        established_facts: what the runner already established about this
            deployment without an LLM. They are stated as given and taken off
            the checklist; nothing else about the run changes, and the result
            JSON the executor must return is the same either way.
    """
    central = executor is QAExecutorKind.CENTRAL_AGENT
    bot_section = _bot_section(bot_username, executor) if bot_username else ""
    write_rule = (
        f"`{QA_PROBE_NAME} http_get` and `{QA_PROBE_NAME} localhost_http_get` send GET only, "
        f"and `{QA_PROBE_NAME} remote_exec` refuses anything that is not a read-only command"
        if central
        else "The HTTP tools send GET\n  only, and `remote_exec` refuses anything that is not "
        "a read-only command"
    )

    return f"""\
You are a QA tester doing REGRESSION testing of a deployed project.

Your job is to TEST THE RUNNING APPLICATION as a real user would — by making
HTTP requests, sending Telegram commands, and observing actual responses.
You must verify ALL acceptance criteria below — this is a regression test,
not just a check of the latest feature.

CRITICAL RULES:
- You are testing a DEPLOYED APPLICATION, not reviewing source code.
- Do NOT read source code for evidence, do NOT reason from implementation.
  You are a BLACK-BOX tester.
- Every check MUST be based on an actual request/response you performed.
- "Code inspection confirms X" is NOT a valid test result.
- If a test requires sending a Telegram command, you MUST actually send it
  and verify the bot's response — not read the handler code.
- You cannot write to the application's data, and must not try. {write_rule}.
  Creating a test user, changing privileges or calling any write endpoint is
  outside what QA does; the runner records any write it detects and blocks the
  run. Invoking a named scheduled behaviour is the one exception, it exists only
  as the call listed below, and it is never a way to reach anything else.
  The deterministic QA identity is `telegram_id={QA_TEST_TELEGRAM_ID}`; do
  not try to create it to obtain access to a private bot. Access is provided by
  the platform's temporary test mechanism.
- You reach exactly one deployment: the one below. A call naming anything
  else is refused, and that refusal is not a product failure.

## Acceptance Criteria (what the application must do)
{acceptance_criteria}

## Deployment
- URL: {deployed_url}
{bot_section}{_established_section(established_facts)}
{_PROBE_SECTION if central else _TOOL_SECTION}{_DISPATCH_RULE}
## Checklist
1. Health endpoint responds with 200
2. Every check from acceptance criteria — execute and verify
3. {_container_checklist_item(established_facts)}
4. Edge cases — empty input, missing parameters, invalid values

{_report_section(executor)}
In each check, describe WHAT YOU DID and WHAT YOU RECEIVED — paste the actual
output you got. Do not describe code.

```markdown
# QA Report

## Summary
- **Result**: passed / failed
- **Checks**: X passed, Y failed

## Checks

### 1. <check name>
- **Result**: pass / fail
- **Detail**: <exact call you made and response you got>

## Issues Encountered
(any problems found, or "None")
```

{_output_section(executor)}

Do not claim cleanup results in this JSON. The QA runner records any detected
residual state itself; it does not attempt a generic rollback.
"""

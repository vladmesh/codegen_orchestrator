"""No secret of a QA run reaches the executor or the run's evidence through a call.

A QA run handles five kinds of secret: up to three of the product's
capabilities (caller identity, users grant, jobs fire), the QA Telegram
credentials the sandbox is handed, and the capability endpoint's run token.
A product can echo a capability — in a body, a log line, an error — and an
executor can repeat the credential or the token it holds. Every row below is
one call of `build_qa_callables` whose product-side answer, or whose executor
submission, carries all five, and asserts that none of them reaches the
executor-facing return, the trace, the observations, the retained evidence or
the log lines. All five sit in the run's one `QARunRedaction`, added where each
enters: the stored capabilities up front, the Telegram credentials as
`run_qa_centrally` adds them, the token where the endpoint mints it.

The table covers every call `build_qa_callables` builds, and fails when one is
added without a row, so a later call cannot slip past the boundary unexamined.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from structlog.testing import capture_logs

from shared.contracts.acceptance import parse_scheduled_behaviours
from src.agents.qa import tools as qa_tools
from src.agents.qa.caller_identity import QACallerIdentity
from src.agents.qa.capability_service import QACapabilityService
from src.agents.qa.tools import QAJobsCapability, build_qa_callables
from src.clients.product_jobs import GeneratedServiceJobsClient
from src.consumers._qa_redaction import REDACTED, TELEGRAM_CREDENTIAL, QARunRedaction
from src.consumers._qa_target import STATUS_MARKER, QACapabilities, QATarget, QATargetSession
from src.consumers._qa_workspace import QAWorkspace

IDENTITY = "identity-capability-reflected-back"  # noqa: S105
GRANT = "grant-capability-reflected-back"  # noqa: S105
JOBS = "jobs-capability-reflected-back"  # noqa: S105
SESSION = "telethon-session-string-handed-over"  # noqa: S105
API_HASH = "telethon-api-hash-handed-over"  # noqa: S105
CAPABILITIES_HELD = (IDENTITY, GRANT, JOBS)
#: The run token is minted by the endpoint each test starts, so it is read late.
_RUN = SimpleNamespace(token="")
#: The marker a row's executor submission carries where it repeats every secret.
SECRETS = "<every secret of the run>"


def _held() -> tuple[str, ...]:
    return (*CAPABILITIES_HELD, SESSION, API_HASH, _RUN.token)


def _reflected() -> str:
    """Everything a product or an executor might echo, in one string."""
    return (
        f"X-Identity-Capability: {IDENTITY} X-Grant-Capability: {GRANT} X-Jobs: {JOBS} "
        f"session={SESSION} api_hash={API_HASH} QA_CAPABILITY_TOKEN={_RUN.token}"
    )


def _filled(value):
    if isinstance(value, str):
        return value.replace(SECRETS, _reflected())
    if isinstance(value, list):
        return [_filled(item) for item in value]
    if isinstance(value, dict):
        return {key: _filled(item) for key, item in value.items()}
    return value


def _redaction() -> QARunRedaction:
    """The run's one set, as the consumer and `run_qa_centrally` fill it."""
    redaction = QARunRedaction.from_stored(
        {
            "USER_IDENTITY_CAPABILITY": IDENTITY,
            "USERS_GRANT_CAPABILITY": GRANT,
            "JOBS_FIRE_CAPABILITY": JOBS,
        }
    )
    redaction.add(SESSION, API_HASH, label=TELEGRAM_CREDENTIAL)
    return redaction


DEPLOYED_URL = "https://reminders.example.com"
CONTAINER = "reminders-backend-1"
CRITERIA = '- FIRE JOB reminders.tick WITH {"at": "2999-01-01T00:00:00Z"} THEN GET /reminders\n'


class _ReflectingConn:
    """A target whose every answer carries the reflected capabilities on both streams."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def run(self, command, *, check=False, timeout=None):
        self.commands.append(command)
        stdout = f"{_reflected()}\n"
        if "curl" in command:
            stdout += f"{STATUS_MARKER}200>>"
        return SimpleNamespace(exit_status=0, stdout=stdout, stderr=f"warning: {_reflected()}")


def _session() -> QATargetSession:
    target = QATarget(
        server_ip="1.2.3.4",
        ssh_user="root",
        qa_ssh_user="qa-observer",
        server_handle="vps-1",
        project_name="reminders",
        deployed_url=DEPLOYED_URL,
        allocated_ports=frozenset({8000}),
    )
    return QATargetSession(
        target,
        _ReflectingConn(),
        QACapabilities(
            deployed_url=DEPLOYED_URL,
            physical_root="/srv/deployments/reminders",
            containers=frozenset({CONTAINER}),
            loopback_ports=frozenset({8000}),
            bot_username="reminders_bot",
        ),
    )


def _product(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/down":
        raise httpx.ConnectError(f"connection reset: {_reflected()}")
    status = 500 if request.url.path == "/broken" else 200
    return httpx.Response(status, text=f"debug: {_reflected()}", headers={"X-Debug": _reflected()})


def _jobs_core() -> AsyncMock:
    """A jobs core that records the capabilities into the command it answers with."""
    transport = AsyncMock()
    transport.request.return_value = httpx.Response(
        200,
        json={
            "contract_version": 1,
            "command_id": "qa-qa-run-1-reminders.tick",
            "name": "reminders.tick",
            "arguments": {"at": "2999-01-01T00:00:00Z", "echo": _reflected()},
            "fired_by_product": "project-1",
            "fired_by_run": "qa-run-1",
            "dispatch_status": "dispatched",
            "accepted_at": "2026-10-03T10:00:00Z",
            "dispatched_at": "2026-10-03T10:00:01Z",
        },
        request=httpx.Request("POST", f"{DEPLOYED_URL}/jobs/fire"),
    )
    return transport


async def _bot(script, *, env, timeout):
    """A bot that answers with the capabilities in its reply, and a noisy child process."""
    reply = {
        "action": "message",
        "attempted": "send /start to @reminders_bot",
        "sent": "/start",
        "delivered": True,
        "replies": [
            {
                "id": 7,
                "text": _reflected(),
                "caption": None,
                "media_type": None,
                "reply_markup": {
                    "type": "ReplyInlineMarkup",
                    "buttons": [
                        {
                            "row": 0,
                            "column": 0,
                            "text": "Details",
                            "type": "KeyboardButtonCallback",
                            "callback_data": "ZGV0YWlscw==",
                        }
                    ],
                },
            }
        ],
        "callback": None,
        "error": None,
    }
    if "GetBotCallbackAnswerRequest" in script:
        reply = {
            **reply,
            "action": "callback",
            "callback": {"text": _reflected(), "alert": False, "url": None},
        }
    return SimpleNamespace(
        exit_status=0,
        stdout=f"telegram_probe_result:{json.dumps(reply)}\n",
        stderr=f"telethon: {_reflected()}",
    )


def _calls(tmp_path: Path) -> tuple[dict, QAWorkspace, QACapabilityService]:
    """The run's calls and the endpoint serving them, all reading one set."""
    redaction = _redaction()
    workspace = QAWorkspace(path=tmp_path)
    workspace.trace_path.touch()
    calls = build_qa_callables(
        session=_session(),
        workspace=workspace,
        telethon_env={
            "TELETHON_SESSION": SESSION,
            "TELETHON_API_ID": "1",
            "TELETHON_API_HASH": API_HASH,
        },
        probe_runner=_bot,
        jobs=QAJobsCapability(
            base_url=DEPLOYED_URL,
            capability=JOBS,
            fired_by_product="project-1",
            fired_by_run="qa-run-1",
            behaviours=tuple(parse_scheduled_behaviours(CRITERIA)),
        ),
        jobs_client_factory=lambda url: GeneratedServiceJobsClient(url, transport=_jobs_core()),
        caller_identity=QACallerIdentity("qa", "central-qa", IDENTITY),
        redaction=redaction,
        http_transport=httpx.MockTransport(_product),
    )
    service = QACapabilityService(
        calls=calls,
        capabilities={},
        submit_verdict=lambda raw: None,
        advertised_host="127.0.0.1",
        redaction=redaction,
    )
    _RUN.token = service.token
    return calls, workspace, service


@dataclass(frozen=True)
class Row:
    tool: str
    #: Calls made before the one under test, so it has something to act on.
    before: tuple[tuple[str, dict], ...]
    args: dict


ROWS = {
    "http_get-body-and-headers": Row("http_get", (), {"path": "/reminders"}),
    "http_get-error-response": Row("http_get", (), {"path": "/broken"}),
    "http_get-transport-error": Row("http_get", (), {"path": "/down"}),
    "localhost_http_get": Row("localhost_http_get", (), {"port": 8000, "path": "/reminders"}),
    "remote_read": Row("remote_read", (), {"path": "logs/app.log"}),
    "remote_exec": Row("remote_exec", (), {"command": ["docker", "top", CONTAINER]}),
    "container_logs": Row("container_logs", (), {"container": CONTAINER}),
    "container_inspect": Row("container_inspect", (), {"container": CONTAINER}),
    "fire_job": Row("fire_job", (), {"name": "reminders.tick"}),
    "job_evidence": Row("job_evidence", (), {"name": "reminders.tick"}),
    "telegram_probe": Row("telegram_probe", (), {"message": "/start"}),
    "telegram_click_button": Row(
        "telegram_click_button",
        (("telegram_probe", {"message": "/start"}),),
        {"message_id": 7, "callback_data": "ZGV0YWlscw=="},
    ),
    # The executor's own submissions, repeating every secret it could print.
    "record_probe": Row(
        "record_probe",
        (),
        {
            "platform": "http",
            "name": "reflect",
            "source": f"print('{SECRETS}')",
            "arguments": [SECRETS],
            "stdout": SECRETS,
            "stderr": SECRETS,
            "exit_status": 0,
            "duration_ms": 5,
        },
    ),
    "write_qa_report": Row("write_qa_report", (), {"markdown": f"# QA\n{SECRETS}"}),
}


def _leaks(text: str) -> list[str]:
    return [value for value in _held() if value in text]


def test_the_table_covers_every_call_the_boundary_builds(tmp_path):
    calls, _, _ = _calls(tmp_path)

    assert {row.tool for row in ROWS.values()} == set(calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("row", list(ROWS.values()), ids=list(ROWS))
async def test_no_secret_reaches_the_executor_or_the_runs_evidence(tmp_path, row):
    _, workspace, service = _calls(tmp_path)

    with capture_logs() as logs:
        for name, args in row.before:
            await service._dispatch(name, args)
        answer = await service._dispatch(row.tool, _filled(row.args))

    evidence = {
        "answer to the executor": json.dumps(answer, default=str),
        "trace": workspace.trace_text(),
        "trace file": workspace.trace_path.read_text(),
        "observations": repr(workspace.observations),
        "telegram evidence": repr(workspace.telegram_probe_evidence),
        "probe runs": repr(workspace.probe_runs),
        "report": workspace.read_report(),
        "logs": repr(logs),
    }
    for where, text in evidence.items():
        assert _leaks(text) == [], where
    # The values were really there — the scrub is what removed them, all kinds.
    scrubbed = "".join(evidence.values())
    assert REDACTED in scrubbed
    assert TELEGRAM_CREDENTIAL in scrubbed
    assert "[redacted: QA capability endpoint token]" in scrubbed


@pytest.mark.asyncio
async def test_without_the_executor_boundary_the_table_would_leak(tmp_path):
    """The rows above bite: take the wrapper away and the reflected values come through."""

    def passthrough(call: Callable, redaction: QARunRedaction) -> Callable:
        return call

    with patch.object(qa_tools, "_at_executor_boundary", passthrough):
        calls, _, _ = _calls(tmp_path)
        answer = await calls["container_logs"](CONTAINER)

    assert _leaks(json.dumps(answer)) == list(_held())


def test_a_value_cut_by_someone_else_leaves_no_fragment():
    """A target's `head -c` or the probe CLI may cut before the scrub sees the text."""
    cut = f"log line {IDENTITY[:-3]}"

    assert IDENTITY[:12] not in _redaction().text(cut)
    assert _redaction().text(cut).endswith(REDACTED)

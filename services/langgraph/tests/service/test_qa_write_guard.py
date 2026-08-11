"""QA-run boundary tests for the read-only application API contract.

The guard used to be a Claude PreToolUse hook filtering a Bash command line.
There is no Bash and no on-target agent any more, so the guarantee is enforced
one layer lower: the tools cannot express a write. These tests put a real HTTP
server behind the "deployed application", drive the real tool set against it
through a real shell on the "target", and require that the server never sees a
method other than GET.

The second half is the fail-closed half: if a write ever does turn up in the
evidence the runner owns, the run is quarantined with a residual trace rather
than reported as a QA result.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import subprocess
from threading import Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("API_BASE_URL", "http://localhost:8001")
os.environ.setdefault("INTERNAL_API_KEY", "test-key")

from shared.contracts.dto.run_result import QABlockerCategory, QARunResult
from shared.contracts.queues.qa import QAServerInfo
from src.agents.qa.tools import build_qa_tools
from src.consumers._qa_runner import QARuntimeConfig, run_qa_centrally
from src.consumers._qa_target import QATarget, QATargetSession
from src.consumers._qa_workspace import qa_workspace
from src.consumers.qa import process_qa_job


class _LocalShellConn:
    """A "target" that really runs what the typed tools ask it to run."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def run(self, command: str, *, check: bool = False, timeout: float | None = None):
        self.commands.append(command)
        proc = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            check=False,
        )
        return SimpleNamespace(stdout=proc.stdout, stderr=proc.stderr, exit_status=proc.returncode)


class _RecordingApplication:
    """The deployed application, recording every method it is asked for."""

    def __init__(self) -> None:
        self.methods: list[str] = []
        application = self

        class Handler(BaseHTTPRequestHandler):
            def _record(self):
                application.methods.append(self.command)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            do_GET = _record
            do_POST = _record
            do_PUT = _record
            do_PATCH = _record
            do_DELETE = _record

            def log_message(self, format, *args):  # noqa: A002
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
        return False

    @property
    def port(self) -> int:
        return self._server.server_port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture(autouse=True)
async def _clean_redis():
    """This runner-boundary test does not use Redis."""
    yield


# Every shape the old Bash guard had to filter. None of them is expressible now:
# there is no tool that takes a method, and no tool that takes a shell.
WRITE_ATTEMPTS = (
    ["curl", "-X", "POST", "{url}/users"],
    ["curl", "{url}/users", "-X", "PATCH", "-d", "{}"],
    ["python3", "-c", "import requests; requests.post('{url}/users')"],
    ["sh", "-c", "curl -d '{}' {url}/users"],
    ["wget", "--method=DELETE", "{url}/users"],
)


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", WRITE_ATTEMPTS)
async def test_no_tool_can_send_a_write_to_the_application(tmp_path, attempt):
    conn = _LocalShellConn()
    with _RecordingApplication() as application:
        target = QATarget(
            server_ip="127.0.0.1",
            ssh_user="qa",
            project_name="app",
            deployed_url=application.url,
        )
        session = QATargetSession(target, conn)
        argv = [part.replace("{url}", application.url) for part in attempt]

        with qa_workspace(root=str(tmp_path)) as workspace:
            tools = {
                tool.name: tool for tool in build_qa_tools(session=session, workspace=workspace)
            }
            refusal = await tools["remote_exec"].ainvoke({"command": argv})
            # The read the agent is entitled to still works, and really reaches
            # the application — so an empty method list would prove nothing.
            await tools["http_get"].ainvoke({"path": "/health"})

        assert "error" in refusal
        assert application.methods == ["GET"]
    # Nothing was even sent to the target: the refusal happens before the wire.
    assert conn.commands == []


@pytest.mark.asyncio
async def test_the_public_probe_is_get_only(tmp_path):
    with _RecordingApplication() as application:
        session = QATargetSession(
            QATarget(
                server_ip="127.0.0.1",
                ssh_user="qa",
                project_name="app",
                deployed_url=application.url,
            ),
            _LocalShellConn(),
        )
        with qa_workspace(root=str(tmp_path)) as workspace:
            tools = {
                tool.name: tool for tool in build_qa_tools(session=session, workspace=workspace)
            }
            assert "method" not in tools["http_get"].args
            answer = await tools["http_get"].ainvoke({"path": "/users"})

        assert answer["status"] == 200
        assert application.methods == ["GET"]


class _WritingAgent:
    """An agent that claims a write in its own result — the fail-closed case."""

    def __init__(self, deployed_url: str) -> None:
        self.deployed_url = deployed_url

    async def ainvoke(self, state, config=None):
        return {
            "messages": [
                SimpleNamespace(
                    content=(
                        '{"pass": true, "checks": [{"name": "signup", "pass": true, "detail": '
                        f'"POST {self.deployed_url}/users returned 201"}}], "summary": "OK"}}'
                    )
                )
            ]
        }


class _FakeTargetConn:
    """Answers the grant commands so the run reaches the agent."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def run(self, command: str, *, check: bool = False, timeout: float | None = None):
        self.commands.append(command)
        if command.startswith("grep -c -F"):
            return SimpleNamespace(exit_status=0, stdout="0\n", stderr="")
        return SimpleNamespace(exit_status=0, stdout="", stderr="")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _writing_graph(deployed_url: str):
    def create(*, model, base_url, api_key, tools, prompt):
        return _WritingAgent(deployed_url)

    return create


@pytest.mark.asyncio
async def test_a_claimed_write_blocks_the_run_with_a_residual_trace(tmp_path):
    """Evidence of a write fails the run closed, even when the agent says pass."""
    conn = _FakeTargetConn()
    with (
        patch("src.consumers._qa_target._connect", AsyncMock(return_value=conn)),
        patch("src.consumers._qa_target._import", lambda key: key),
        patch("src.consumers._qa_workspace.QA_WORKSPACE_ROOT", str(tmp_path / "runs")),
        patch("src.consumers._qa_runner.create_qa_graph", _writing_graph("http://app.example")),
    ):
        result = await run_qa_centrally(
            target=QATarget(
                server_ip="1.2.3.4",
                ssh_user="qa",
                project_name="app",
                deployed_url="http://app.example",
            ),
            fleet_ssh_key="fleet-key",
            acceptance_criteria="- read-only check",
            runtime=QARuntimeConfig(model="m", base_url="u", api_key="k"),
        )

    assert result.passed is False
    assert result.blocker is not None
    assert result.blocker.category is QABlockerCategory.UNKNOWN
    assert result.state_changes[0]["resource"] == "POST http://app.example/users"
    assert result.state_changes[0]["cleanup"]["succeeded"] is False


@pytest.mark.asyncio
async def test_qa_consumer_quarantines_a_write_trace(tmp_path):
    """The consumer persists the runner-created trace as a quarantine blocker."""
    conn = _FakeTargetConn()
    redis = AsyncMock()
    redis.redis.set = AsyncMock(return_value=True)
    redis.redis.delete = AsyncMock()
    api_client = AsyncMock()
    api_client.patch = AsyncMock()
    api_client.get_project = AsyncMock(return_value=SimpleNamespace(slug="app", config={}))

    with (
        patch("src.consumers.qa.api_client", api_client),
        patch(
            "src.consumers.qa.check_deployed_url_reachable",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "src.consumers.qa._resolve_server_info",
            new_callable=AsyncMock,
            return_value=QAServerInfo(
                server_ip="1.2.3.4",
                ssh_user="qa",
                ssh_key="fake",
                project_name="app",
            ),
        ),
        patch("src.consumers.qa.get_settings") as get_settings,
        patch("src.consumers._qa_target._connect", AsyncMock(return_value=conn)),
        patch("src.consumers._qa_target._import", lambda key: key),
        patch("src.consumers._qa_workspace.QA_WORKSPACE_ROOT", str(tmp_path / "runs")),
        patch("src.consumers._qa_runner.create_qa_graph", _writing_graph("http://app.example")),
    ):
        get_settings.return_value = SimpleNamespace(
            qa_llm_model="m", qa_llm_base_url="u", qa_llm_api_key="k"
        )
        await process_qa_job(
            {
                "story_id": "story-1",
                "project_id": "project-1",
                "telegram_chat_id": "1",
                "deployed_url": "http://app.example",
                "application_id": 1,
                "acceptance_criteria": "- bot replies to /start",
                "run_id": "qa-run-1",
                "qa_attempt": 0,
            },
            redis,
        )

    persisted = api_client.patch.await_args_list[-1].kwargs["json"]["result"]
    result = QARunResult.model_validate(persisted)
    assert result.qa_outcome.value == "blocked"
    assert result.blocker is not None
    assert result.state_changes[0].resource == "POST http://app.example/users"
    assert result.state_changes[0].cleanup.succeeded is False

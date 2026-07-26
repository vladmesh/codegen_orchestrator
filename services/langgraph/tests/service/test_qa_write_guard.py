"""QA-run boundary tests for the read-only application API contract."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
from threading import Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("API_BASE_URL", "http://localhost:8001")
os.environ.setdefault("INTERNAL_API_KEY", "test-key")

from shared.contracts.dto.run_result import QABlockerCategory, QARunResult
from shared.contracts.queues.qa import QAServerInfo
from src.consumers._qa_runner import run_qa_on_server
from src.consumers.qa import process_qa_job

GUARD = Path(__file__).parents[3] / "infra-service/ansible/roles/qa_runner/files/qa-write-guard.py"


@pytest.fixture(autouse=True)
async def _clean_redis():
    """This runner-boundary test does not use Redis."""
    yield


@pytest.mark.parametrize(
    "command, expected",
    [
        ("curl -XPOST http://app.example/users", "POST http://app.example/users"),
        (
            "curl http://app.example/users -X PATCH -d '{}'",
            "PATCH http://app.example/users",
        ),
        (
            "python -c \"requests.request('POST', 'http://app.example/users')\"",
            "POST http://app.example/users",
        ),
        (
            "python -c \"httpx.request('DELETE', 'http://app.example/users')\"",
            "DELETE http://app.example/users",
        ),
    ],
)
def test_runner_write_hook_executes_and_records_direct_application_write(
    tmp_path, command: str, expected: str
):
    """The Claude Bash hook prevents a real Bash write from reaching the app."""
    trace = tmp_path / "writes.jsonl"
    received_requests: list[str] = []

    class ApplicationHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            received_requests.append(self.command)
            self.send_response(201)
            self.end_headers()

        def do_PUT(self):  # noqa: N802
            received_requests.append(self.command)
            self.send_response(200)
            self.end_headers()

        def do_PATCH(self):  # noqa: N802
            received_requests.append(self.command)
            self.send_response(200)
            self.end_headers()

        def do_DELETE(self):  # noqa: N802
            received_requests.append(self.command)
            self.send_response(204)
            self.end_headers()

        def log_message(self, format, *args):  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), ApplicationHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    target = f"http://127.0.0.1:{server.server_port}"
    command = command.replace("http://app.example", target)
    expected = expected.replace("http://app.example", target)

    result = subprocess.run(
        [str(GUARD), "--target", target, "--trace", str(trace)],
        input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True,
        text=True,
    )

    try:
        # This models Claude's PreToolUse contract: a non-zero hook result
        # rejects the Bash tool call, so the command below must not run.
        if result.returncode == 0:
            subprocess.run(["bash", "-c", command], check=True)

        assert result.returncode == 2
        assert trace.read_text().strip() == expected
        assert received_requests == []
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join()


@pytest.mark.asyncio
async def test_qa_run_write_hook_trace_is_blocked_and_persists_residual_trace():
    """A hook trace wins over normal agent JSON and makes the run unverified."""
    command_result = SimpleNamespace(
        stdout='{"pass": true, "checks": [], "summary": "passed"}',
        stderr="",
        exit_status=0,
    )
    conn = AsyncMock()
    conn.run = AsyncMock(return_value=command_result)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("src.consumers._qa_runner.asyncssh") as mock_asyncssh,
        patch(
            "src.consumers._qa_runner._preflight_agent_qa",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("src.consumers._qa_runner._ensure_claude_credentials", new_callable=AsyncMock),
        patch(
            "src.consumers._qa_runner._collect_qa_report",
            new_callable=AsyncMock,
            return_value="# QA Report\n- no direct writes\n",
        ),
        patch(
            "src.consumers._qa_runner._collect_qa_write_guard_trace",
            new_callable=AsyncMock,
            return_value="POST http://app.example/users",
        ),
    ):
        mock_asyncssh.import_private_key.return_value = "parsed_key"
        mock_asyncssh.connect.return_value = conn
        result = await run_qa_on_server(
            server_ip="1.2.3.4",
            ssh_user="qa",
            ssh_key="fake",
            project_name="app",
            acceptance_criteria="- read-only check",
            deployed_url="http://app.example",
        )

    assert result.passed is False
    assert result.blocker is not None
    assert result.blocker.category is QABlockerCategory.UNKNOWN
    assert result.state_changes[0]["resource"] == "POST http://app.example/users"
    assert result.state_changes[0]["cleanup"]["succeeded"] is False


@pytest.mark.asyncio
async def test_qa_consumer_persists_direct_write_trace_as_a_quarantine_ready_blocker():
    """A real runner violation cannot become a passed QA run downstream."""
    command_result = SimpleNamespace(
        stdout='{"pass": true, "checks": [], "summary": "passed"}',
        stderr="",
        exit_status=0,
    )
    conn = AsyncMock()
    conn.run = AsyncMock(return_value=command_result)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
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
        patch("src.consumers._qa_runner.asyncssh") as mock_asyncssh,
        patch(
            "src.consumers._qa_runner._preflight_agent_qa",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("src.consumers._qa_runner._ensure_claude_credentials", new_callable=AsyncMock),
        patch(
            "src.consumers._qa_runner._collect_qa_report",
            new_callable=AsyncMock,
            return_value="# QA Report\n- normal output\n",
        ),
        patch(
            "src.consumers._qa_runner._collect_qa_write_guard_trace",
            new_callable=AsyncMock,
            return_value="POST http://app.example/users",
        ),
    ):
        mock_asyncssh.import_private_key.return_value = "parsed_key"
        mock_asyncssh.connect.return_value = conn
        await process_qa_job(
            {
                "story_id": "story-1",
                "project_id": "project-1",
                "user_id": "1",
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

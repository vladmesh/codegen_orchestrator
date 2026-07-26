"""QA-run boundary tests for the read-only application API contract."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("API_BASE_URL", "http://localhost:8001")
os.environ.setdefault("INTERNAL_API_KEY", "test-key")

from shared.contracts.dto.run_result import QABlockerCategory, QARunResult
from shared.contracts.queues.qa import QAServerInfo
from src.consumers._qa_runner import run_qa_on_server
from src.consumers.qa import process_qa_job


@pytest.fixture(autouse=True)
async def _clean_redis():
    """This runner-boundary test does not use Redis."""
    yield


@pytest.mark.parametrize(
    "command",
    [
        "curl -X POST http://app.example/users",
        "curl --data '{\"telegram_id\": 8202532144}' http://app.example/users",
    ],
)
@pytest.mark.asyncio
async def test_qa_run_write_attempt_is_blocked_and_persists_residual_trace(command: str):
    """Drive a QA run whose report exposes a direct write to an empty app."""
    command_result = SimpleNamespace(
        stdout='{"pass": true, "checks": [], "summary": "passed"}',
        stderr="",
        exit_status=0,
    )
    report = f"# QA Report\n- command: {command}\n"
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
            return_value=report,
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
            return_value="# QA Report\n- command: curl -X POST http://app.example/users\n",
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

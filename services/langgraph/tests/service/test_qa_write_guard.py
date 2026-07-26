"""QA-run boundary tests for the read-only application API contract."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.run_result import QABlockerCategory
from src.consumers._qa_runner import run_qa_on_server


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

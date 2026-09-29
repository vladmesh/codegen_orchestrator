"""Registered PO command carries server caller context and the observed fence."""

from unittest.mock import AsyncMock

import httpx
import pytest

from src.agents.po import tools, tools_projects


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "disposition,status",
    [
        ("in_flight", "queued"),
        ("already_applied", "applied"),
        ("stale_target", "failed"),
        ("exhausted", "failed"),
    ],
)
async def test_retry_tool_preserves_observed_fence_and_caller(monkeypatch, disposition, status):
    api = AsyncMock()
    api.post_raw.return_value = httpx.Response(
        200,
        request=httpx.Request("POST", "http://api.test/retry"),
        json={
            "intent_id": "native-intent",
            "status": status,
            "disposition": disposition,
        },
    )
    monkeypatch.setattr(tools_projects, "_get_api", lambda: api)
    assert tools_projects.retry_initial_owner_deployment in tools.get_all_tools()
    config = {"configurable": {"telegram_chat_id": "84"}}
    args = {
        "project_id": "native-project",
        "intent_id": "native-intent",
        "expected_execution_run_id": "observed-exhausted-run",
    }
    text = await tools_projects.retry_initial_owner_deployment.ainvoke(args, config=config)
    assert disposition in text
    api.post_raw.assert_awaited_once_with(
        "projects/native-project/users/grant-intents/native-intent/retry",
        json={"expected_execution_run_id": "observed-exhausted-run"},
        headers={"X-Telegram-ID": "84"},
    )
    assert "DEPLOY_" not in text


@pytest.mark.asyncio
async def test_owed_publication_keeps_the_same_fence(monkeypatch):
    api = AsyncMock()
    api.post_raw.return_value = httpx.Response(503, json={"detail": "dispatch owed"})
    monkeypatch.setattr(tools_projects, "_get_api", lambda: api)
    text = await tools_projects.retry_initial_owner_deployment.ainvoke(
        {"project_id": "p", "intent_id": "i", "expected_execution_run_id": "r"},
        config={"configurable": {"telegram_chat_id": "84"}},
    )
    assert "publication is owed" in text and "same fenced command" in text
    api.get_raw.assert_not_awaited()

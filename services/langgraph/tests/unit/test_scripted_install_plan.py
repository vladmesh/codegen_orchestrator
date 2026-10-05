"""No-model planning still supplies the native coverage admission contract."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest

from shared.contracts.dto.story_planning import PlanningChannels
from src import scripted_install_plan as harness
from src.clients.api import LanggraphAPIClient
from tests.unit.test_catalog_install import snapshot


@pytest.mark.asyncio
async def test_scripted_admission_records_no_model_channels(monkeypatch):
    api = create_autospec(LanggraphAPIClient, instance=True)
    api.get_story.return_value = SimpleNamespace(status="in_progress")
    api.admit_product_brief_coverage.return_value = SimpleNamespace(outcome="admitted")
    monkeypatch.setattr(harness, "api_client", api)
    monkeypatch.setattr(
        harness.plan_install,
        "coroutine",
        AsyncMock(return_value={"id": "task-install", "type": "install", "install": {}}),
    )
    monkeypatch.setattr(
        harness.record_requirement_coverage, "coroutine", AsyncMock(return_value={})
    )
    result = await harness._owned_plan(
        snapshot(),
        SimpleNamespace(id="brief-1"),
        "plan-1",
        "project-1",
        "story-1",
        "reminders",
        ["r1"],
    )
    assert result["coverage_outcome"] == "admitted"
    api.admit_product_brief_coverage.assert_awaited_once_with(
        "brief-1", "plan-1", channels=PlanningChannels()
    )

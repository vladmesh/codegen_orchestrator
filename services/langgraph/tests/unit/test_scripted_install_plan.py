"""No-model planning still supplies the native coverage admission contract."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest

from shared.contracts.dto.story_planning import PlanningChannels
from src import scripted_install_plan as harness
from src.clients.api import LanggraphAPIClient
from tests.unit.test_catalog_install import snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("active,owned", [(False, True), (True, False)])
async def test_transferred_claim_refuses_stale_or_foreign_attempt(monkeypatch, active, owned):
    api = create_autospec(LanggraphAPIClient, instance=True)
    api.get_product_brief_by_story.return_value = SimpleNamespace(
        id="brief-1",
        project_id="project-1",
        confirmed_at=True,
        must_requirements=[],
        content=SimpleNamespace(must_requirements=[]),
        planning_attempt_active=active,
        planning_attempt_id="plan-1" if owned else "other",
    )
    monkeypatch.setattr(harness, "api_client", api)
    result = await harness.scripted_install_plan(
        "project-1", "story-1", "reminders", [], planning_attempt_id="plan-1"
    )
    assert result == {"error": "planning_claim_not_owned"}
    api.claim_planning_attempt.assert_not_awaited()
    api.finish_planning_attempt.assert_not_awaited()


@pytest.mark.asyncio
async def test_transferred_claim_uses_live_catalog_and_finishes_same_attempt(monkeypatch):
    api = create_autospec(LanggraphAPIClient, instance=True)
    brief = SimpleNamespace(
        id="brief-1",
        project_id="project-1",
        confirmed_at=True,
        content=SimpleNamespace(must_requirements=[SimpleNamespace(id="r1")]),
        planning_attempt_active=True,
        planning_attempt_id="plan-1",
    )
    api.get_product_brief_by_story.return_value = brief
    reader = SimpleNamespace(read=AsyncMock(return_value=snapshot()))
    owned = AsyncMock(return_value={"coverage_outcome": "admitted"})
    monkeypatch.setattr(harness, "api_client", api)
    monkeypatch.setattr(harness, "get_kit_catalog_reader", lambda: reader)
    monkeypatch.setattr(harness, "_owned_plan", owned)
    await harness.scripted_install_plan(
        "project-1", "story-1", "reminders", ["r1"], planning_attempt_id="plan-1"
    )
    api.claim_planning_attempt.assert_not_awaited()
    owned.assert_awaited_once_with(
        reader.read.return_value, brief, "plan-1", "project-1", "story-1", "reminders", ["r1"]
    )
    api.finish_planning_attempt.assert_awaited_once_with("brief-1", "plan-1")


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


@pytest.mark.asyncio
async def test_owned_po_claim_is_taken_before_architect_publication(monkeypatch):
    from unittest.mock import MagicMock

    from src.agents.po import tools_stories

    api = MagicMock()
    api.post_raw = AsyncMock(
        return_value=SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"outcome": "claimed", "planning_attempt_id": "plan-1"},
        )
    )
    claim = await tools_stories.claim_scripted_plan(api, "brief-1", {"X-Telegram-ID": "42"})
    assert claim == "plan-1"
    api.post_raw.assert_awaited_once_with(
        "product-briefs/brief-1/planning-attempts/claim", headers={"X-Telegram-ID": "42"}
    )
    api.post_raw.return_value.json = lambda: {
        "outcome": "in_progress",
        "planning_attempt_id": "foreign",
    }
    with pytest.raises(RuntimeError, match="planning_in_progress"):
        await tools_stories.claim_scripted_plan(api, "brief-1", {})

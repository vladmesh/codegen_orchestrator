"""A confirmed brief's stored capability plan is planned as stored, or not at all.

The first planning attempt of a capability-backed brief has no INSTALL task yet; what it
installs comes from the plan stored beside the confirmed revision, never from a model's
choice or a remembered catalog. Everything past the API boundary is real: the consumer,
the activated catalog's genuine bytes (`activated_kit_catalog`), `plan_install_payload`,
`create_install_task`, coverage and admission over the in-memory brief boundary. The plan
itself is the real preview and derivation the PO tool and the API make.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest

from shared.catalog_activation import CATALOG_ACTIVATION
from shared.contracts.dto.capability_preview import (
    BriefCapabilities,
    CapabilityPlan,
    CapabilityRequest,
    derive_capability_plan,
)
from shared.contracts.dto.product_brief import ProductBriefContent
from src.capability_feasibility import platform_cannot
from src.capability_preview import capability_id, resolve_preview
from src.kit_catalog import KitCatalogFailure, KitCatalogUnavailable
from tests.unit.architect_kit_catalog import CatalogBrief, CatalogPlanApi, plan_story

CHANNELS = capability_id("tg-channels")
PREVIEW_ID = "preview-" + "c" * 24


def _brief(*, with_scratch: bool) -> tuple[CatalogBrief, CapabilityPlan]:
    requests = [
        CapabilityRequest(request_id="channels", capability_id=CHANNELS, wording="Cyprus news"),
    ]
    capabilities = [
        {
            "request_id": "channels",
            "capability_id": CHANNELS,
            "route": "module",
            "requirement_ids": ["digest"],
        }
    ]
    requirements = [
        {"id": "digest", "text": "Shows a digest of my channels", "user_wording": "digest"}
    ]
    examples = [
        {"requirement_id": "digest", "user_sends": "/digest", "product_answers": "Latest posts"}
    ]
    if with_scratch:
        requests.append(CapabilityRequest(request_id="notes", wording="keep notes"))
        capabilities.append(
            {"request_id": "notes", "route": "from_scratch", "requirement_ids": ["notes"]}
        )
        requirements.append({"id": "notes", "text": "Keeps notes", "user_wording": "notes"})
        examples.append(
            {"requirement_id": "notes", "user_sends": "/note milk", "product_answers": "Saved"}
        )
    brief_capabilities = BriefCapabilities.model_validate(
        {
            "preview_id": PREVIEW_ID,
            "capabilities": capabilities,
            "answers": [
                {
                    "question_id": "product_language",
                    "kind": "product_language",
                    "value": "en",
                    "description": "English",
                }
            ],
        }
    )
    content = ProductBriefContent.model_validate(
        {
            "summary": "Channel digests",
            "language": "en",
            "must_requirements": requirements,
            "usage_examples": examples,
            "capabilities": brief_capabilities.model_dump(),
        }
    )
    return CatalogBrief("Channels bot", "A digest bot", content), requests


def _stored_plan(catalog, brief: CatalogBrief, requests) -> CapabilityPlan:
    preview = resolve_preview(
        project_id=uuid.uuid4(),
        requests=requests,
        catalog=catalog,
        activation=CATALOG_ACTIVATION,
        project_modules={"backend", "tg_bot"},
        rollout_admits=True,
        platform_cannot=platform_cannot,
    )
    return derive_capability_plan(
        preview_id=PREVIEW_ID,
        product=preview.product,
        technical=preview.technical,
        capabilities=brief.content.capabilities,
        must_requirement_ids={r.id for r in brief.content.must_requirements},
        initial_setting_keys=set(),
        activation=CATALOG_ACTIVATION,
    )


class _PlanApi(CatalogPlanApi):
    def __init__(self, brief: CatalogBrief, plan: CapabilityPlan | None):
        super().__init__(brief)
        self.plan = plan

    async def get_capability_plan(self, brief_id):
        return self.plan


def _no_graph(*_args, **_kwargs):
    raise AssertionError("a fully planned capability brief must not build a model graph")


async def _plan(api: _PlanApi, graph=_no_graph) -> dict:
    with patch("src.consumers.architect.create_architect_graph", side_effect=graph):
        return await plan_story(api, model="scripted", base_url="http://llm.invalid", api_key="x")


@pytest.fixture
def module_brief(activated_kit_catalog, kit_catalog_off_github):
    kit_catalog_off_github.read.return_value = activated_kit_catalog
    brief, requests = _brief(with_scratch=False)
    return brief, _stored_plan(activated_kit_catalog, brief, requests)


@pytest.mark.asyncio
async def test_a_first_attempt_installs_the_stored_closure_without_a_model(module_brief):
    brief, plan = module_brief
    api = _PlanApi(brief, plan)

    result = await _plan(api)

    assert result["status"] == "success", result
    [task] = api.task_payloads
    assert task["type"] == "install"
    assert task["install"] == plan.capabilities[0].install.model_dump(mode="json")
    assert task["install"]["catalog"]["commit"] == CATALOG_ACTIVATION.commit
    assert task["planning_attempt_id"] == "plan-live"
    assert api.coverage == {"digest": ("plan-live", "task-1", None)}
    assert api.admit_calls == 1 and api.released == ["task-1"]


@pytest.mark.asyncio
async def test_the_rest_of_the_brief_is_planned_by_the_model_around_the_stored_install(
    activated_kit_catalog, kit_catalog_off_github
):
    kit_catalog_off_github.read.return_value = activated_kit_catalog
    brief, requests = _brief(with_scratch=True)
    api = _PlanApi(brief, _stored_plan(activated_kit_catalog, brief, requests))
    seen: dict = {}

    def graph(_llm):
        async def ainvoke(state, config):
            seen.update(state)
            task = await api.create_task(
                {
                    "title": "Notes",
                    "type": "feature",
                    "story_id": state["story_id"],
                    "planning_attempt_id": "plan-live",
                }
            )
            coverage = MagicMock(requirement_id="notes", planning_attempt_id="plan-live")
            coverage.task_id, coverage.returned_reason = task.id, None
            await api.record_requirement_coverage(api.brief.id, coverage)
            return {"messages": []}

        return MagicMock(ainvoke=AsyncMock(side_effect=ainvoke))

    with patch("src.consumers.architect.load_channel_chain", new=AsyncMock(return_value=[])):
        with patch("src.consumers.architect.unconfigured_channel_env", return_value=[]):
            with patch("src.consumers.architect.build_agent_llm"):
                result = await _plan(api, graph)

    assert result["status"] == "success", result
    assert api.task_payloads[0]["type"] == "install"
    # The model can neither select nor install a package in this run.
    assert seen["kit_install_snapshot"] is None and seen["kit_catalog_packages"] == []
    briefing = seen["messages"][0]["content"]
    assert "Capability plan of the confirmed brief" in briefing
    assert "Already disposed by the capability plan (record nothing for them): digest" in briefing
    assert "- notes: built from scratch for notes" in briefing
    assert "Kit package catalog" not in briefing
    assert set(api.coverage) == {"digest", "notes"}


@pytest.mark.asyncio
async def test_an_unreachable_catalog_delays_the_first_attempt_with_no_task(
    module_brief, kit_catalog_off_github
):
    """A catalog outage never turns a capability brief into agent programming."""
    brief, plan = module_brief
    kit_catalog_off_github.read.return_value = KitCatalogUnavailable(
        "https://kit.invalid", KitCatalogFailure.TRANSPORT, "ConnectError"
    )
    api = _PlanApi(brief, plan)

    result = await _plan(api)

    assert result["status"] == "failed"
    assert api.task_payloads == [] and api.coverage == {} and api.admit_calls == 0
    [report] = api.planning_reports
    assert report.retriable is True and "KitCatalogUnavailable" in report.failure.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [KitCatalogFailure.PROVENANCE, KitCatalogFailure.INACTIVE])
async def test_a_catalog_that_is_not_the_activated_snapshot_parks_the_story(
    module_brief, kit_catalog_off_github, failure
):
    brief, plan = module_brief
    kit_catalog_off_github.read.return_value = KitCatalogUnavailable(
        "https://kit.invalid", failure, "detail"
    )
    api = _PlanApi(brief, plan)

    result = await _plan(api)

    assert result["status"] == "failed" and api.task_payloads == []
    [report] = api.planning_reports
    assert report.retriable is False
    assert f"CapabilityPlanRefused: catalog_{failure.value}" in report.failure.detail


@pytest.mark.asyncio
async def test_a_capability_brief_without_its_stored_plan_is_never_planned(module_brief):
    brief, _plan_stored = module_brief
    api = _PlanApi(brief, None)

    result = await _plan(api)

    assert result["status"] == "failed" and api.task_payloads == []
    [report] = api.planning_reports
    assert report.retriable is False
    assert "capability_plan_missing" in report.failure.detail


@pytest.mark.asyncio
async def test_a_plan_from_another_activation_is_not_planned(module_brief):
    brief, plan = module_brief
    other = plan.model_copy(
        update={"activation": CATALOG_ACTIVATION.model_copy(update={"commit": "0" * 40})}
    )
    api = _PlanApi(brief, other)

    result = await _plan(api)

    assert result["status"] == "failed" and api.task_payloads == []
    assert "catalog_provenance_mismatch" in api.planning_reports[0].failure.detail


@pytest.mark.asyncio
async def test_a_stored_closure_that_drifted_from_the_snapshot_creates_nothing(module_brief):
    brief, plan = module_brief
    install = plan.capabilities[0].install
    drifted = install.model_copy(
        update={
            "package": install.package.model_copy(
                update={"version": "0.1.1", "tag": "packages/tg-channels/v0.1.1"}
            )
        }
    )
    capability = plan.capabilities[0].model_copy(update={"install": drifted})
    api = _PlanApi(brief, plan.model_copy(update={"capabilities": [capability]}))

    result = await _plan(api)

    assert result["status"] == "failed" and api.task_payloads == []
    [report] = api.planning_reports
    assert report.retriable is False and "plan_drift: channels" in report.failure.detail


@pytest.mark.asyncio
async def test_two_selections_of_one_capability_share_one_install(
    activated_kit_catalog, kit_catalog_off_github
):
    kit_catalog_off_github.read.return_value = activated_kit_catalog
    brief, requests = _brief(with_scratch=False)
    requests.append(requests[0].model_copy(update={"request_id": "posts"}))
    content = brief.content.model_dump(mode="json")
    content["must_requirements"].append(
        {"id": "posts", "text": "Delivers new posts", "user_wording": "new posts"}
    )
    content["usage_examples"].append(
        {"requirement_id": "posts", "user_sends": "/digest", "product_answers": "New post"}
    )
    content["capabilities"]["capabilities"].append(
        {
            "request_id": "posts",
            "capability_id": CHANNELS,
            "route": "module",
            "requirement_ids": ["posts"],
        }
    )
    brief = CatalogBrief(
        brief.title, brief.description, ProductBriefContent.model_validate(content)
    )
    plan = _stored_plan(activated_kit_catalog, brief, requests)
    api = _PlanApi(brief, plan)

    result = await _plan(api)

    assert result["status"] == "success", result
    [task] = api.task_payloads
    assert task["install"] == plan.capabilities[0].install.model_dump(mode="json")
    assert api.coverage == {
        "digest": ("plan-live", "task-1", None),
        "posts": ("plan-live", "task-1", None),
    }

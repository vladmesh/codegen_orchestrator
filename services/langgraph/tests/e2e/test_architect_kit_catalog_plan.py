"""E2E: the real architect resolves two briefs against the kit's live package catalog.

Requires a real LLM API key and reaches the kit's live catalog over HTTP. Run with:

    ARCHITECT_LLM_MODEL=openai/gpt-5.6-sol \
    ARCHITECT_LLM_BASE_URL=https://openrouter.ai/api/v1 \
    ARCHITECT_LLM_API_KEY=$OPENROUTER_API_KEY \
    pytest services/langgraph/tests/e2e/test_architect_kit_catalog_plan.py -v -s

The model is real and runs through the architect consumer, graph and tools, and the
catalog is the production reader at its configured source and ref (by default the
kit's default branch). The API behind them is the in-memory stub of the scripted run,
so nothing is written anywhere. The checks are the ones the scripted run applies
(`tests/unit/test_architect_kit_catalog_plan.py`).
"""

from __future__ import annotations

import os

import pytest

from src.agents.architect.tools import reset_task_chain
from src.config.agent_llm_env import AGENT_LLM_ENV
from src.kit_catalog import KitCatalog, get_kit_catalog_reader
from tests.unit.architect_kit_catalog import (
    REMINDERS,
    CatalogPlanApi,
    assert_every_requirement_is_disposed,
    assert_plan_installs_no_package,
    assert_plan_installs_reminders_from_the_catalog,
    plan_story,
    reminders_brief,
    shopping_list_brief,
)


def _llm_config() -> dict:
    names = AGENT_LLM_ENV["architect"]
    values = [os.getenv(name) for name in names]
    if not all(values):
        pytest.skip(f"{', '.join(names)} required for E2E tests")
    return dict(zip(("model", "base_url", "api_key"), values, strict=True))


async def _live_catalog() -> KitCatalog:
    catalog = await get_kit_catalog_reader().read()
    if not isinstance(catalog, KitCatalog):
        pytest.fail(f"the live kit catalog is unavailable: {catalog}")
    return catalog


async def _plan(api: CatalogPlanApi, config: dict) -> tuple[dict, str]:
    reset_task_chain()
    result = await plan_story(api, **config)
    report = (
        f"result={result}\ncoverage={api.coverage}\ncriteria=\n{api.criteria}\n"
        f"tasks={[(t['description'], t['acceptance_criteria']) for t in api.task_payloads]}"
    )
    print(report)
    return result, report


@pytest.mark.asyncio
async def test_the_real_architect_installs_reminders_from_the_live_catalog():
    config = _llm_config()
    catalog = await _live_catalog()
    assert REMINDERS in catalog.names, f"the live catalog lists no {REMINDERS}: {catalog}"
    api = CatalogPlanApi(reminders_brief())

    result, report = await _plan(api, config)

    assert result["status"] == "success", report
    assert_every_requirement_is_disposed(api)
    assert_plan_installs_reminders_from_the_catalog(api)


@pytest.mark.asyncio
async def test_the_real_architect_plans_no_package_no_catalog_entry_covers():
    config = _llm_config()
    await _live_catalog()
    api = CatalogPlanApi(shopping_list_brief())

    result, report = await _plan(api, config)

    assert result["status"] == "success", report
    assert_every_requirement_is_disposed(api)
    assert_plan_installs_no_package(api)

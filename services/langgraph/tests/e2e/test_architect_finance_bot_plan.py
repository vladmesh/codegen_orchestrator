"""E2E plan of the 2026-09-15 finance-bot brief by the real architect prompt.

Requires a real LLM API key. Run with:

    ARCHITECT_LLM_MODEL=openai/gpt-5.6-sol \
    ARCHITECT_LLM_BASE_URL=https://openrouter.ai/api/v1 \
    ARCHITECT_LLM_API_KEY=$OPENROUTER_API_KEY \
    pytest services/langgraph/tests/e2e/test_architect_finance_bot_plan.py -v -s

The model is real and runs through the architect consumer, graph and tools; the
API behind them is the in-memory stub of the scripted run, so nothing is
written anywhere. The checks are the ones the scripted run applies
(`tests/unit/test_architect_finance_bot_plan.py`).
"""

from __future__ import annotations

import os

import pytest

from src.agents.architect.tools import reset_task_chain
from src.config.agent_llm_env import AGENT_LLM_ENV
from tests.unit.architect_finance_bot import (
    FinanceBotApi,
    assert_plan_uses_exactly_the_confirmed_examples,
    finance_bot_brief,
    plan_finance_bot,
)


def _llm_config() -> dict:
    names = AGENT_LLM_ENV["architect"]
    values = [os.getenv(name) for name in names]
    if not all(values):
        pytest.skip(f"{', '.join(names)} required for E2E tests")
    return dict(zip(("model", "base_url", "api_key"), values, strict=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("income_free_text", ["undefined", "example"])
async def test_the_real_architect_checks_every_example_and_returns_only_an_undefined_input(
    income_free_text,
):
    config = _llm_config()
    api = FinanceBotApi(finance_bot_brief(income_free_text))

    reset_task_chain()
    result = await plan_finance_bot(api, **config)

    report = (
        f"result={result}\ncoverage={api.coverage}\ncriteria=\n{api.criteria}\n"
        f"tasks={[t['description'] for t in api.task_payloads]}"
    )
    print(report)
    assert result["status"] == "success", report
    assert_plan_uses_exactly_the_confirmed_examples(api)

"""E2E replay of the 2026-09-15 finance-bot request against the real PO prompt.

Requires a real LLM API key. Run with:

    PO_LLM_MODEL=openai/gpt-5.6-sol \
    PO_LLM_BASE_URL=https://openrouter.ai/api/v1 \
    PO_LLM_API_KEY=$OPENROUTER_API_KEY \
    pytest services/langgraph/tests/e2e/test_po_finance_bot_brief.py -v -s

The model is real; the Product Brief API behind `present_product_brief` is the
in-memory stand-in of the brief tool tests, so nothing is written anywhere. The
checks are the ones the scripted replay applies
(`tests/unit/po/test_finance_bot_brief_replay.py`).
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import ToolNode, create_react_agent
import pytest

from shared.contracts.dto.product_brief import ProductBriefContent
from src.agents.po.tools_briefs import present_product_brief
from src.agents.po.tools_shared import init_po_clients
from src.prompts.po import SYSTEM_PROMPT
from tests.unit.po.finance_bot_replay import (
    NUDGE,
    TELEGRAM_CHAT_ID,
    USER_MESSAGES,
    income_by_free_text_is_decided,
    ocr_trade_off_is_named,
    user_message,
)
from tests.unit.po.test_tools_briefs import _API, PROJECT_ID

#: How many times the user asks for the brief after the scripted messages.
MAX_NUDGES = 2


def _get_llm_config() -> dict:
    """Get LLM config from env vars, skip if not available."""
    model = os.getenv("PO_LLM_MODEL")
    base_url = os.getenv("PO_LLM_BASE_URL")
    api_key = os.getenv("PO_LLM_API_KEY")

    if not all([model, base_url, api_key]):
        pytest.skip("PO_LLM_MODEL, PO_LLM_BASE_URL, PO_LLM_API_KEY required for E2E tests")

    return {"model": model, "base_url": base_url, "api_key": api_key}


@pytest.mark.asyncio
async def test_the_po_brief_decides_income_by_text_and_names_the_free_ocr_trade_off():
    llm_config = _get_llm_config()
    api = _API()
    init_po_clients(api, AsyncMock())

    graph = create_react_agent(
        model=ChatOpenAI(
            model=llm_config["model"],
            base_url=llm_config["base_url"],
            api_key=llm_config["api_key"],
        ),
        tools=ToolNode([present_product_brief], handle_tool_errors=True),
        prompt=SYSTEM_PROMPT,
        checkpointer=MemorySaver(),
    )
    config = {"configurable": {"thread_id": "po-finance-bot", "telegram_chat_id": TELEGRAM_CHAT_ID}}

    turns = [*USER_MESSAGES, *[NUDGE] * MAX_NUDGES]
    for index, text in enumerate(turns):
        await graph.ainvoke(
            {"messages": [HumanMessage(content=user_message(index, text, PROJECT_ID))]},
            config=config,
        )
        if api.briefs:
            break

    assert api.briefs, "the PO never presented a Product Brief"
    latest = max(api.briefs.values(), key=lambda brief: brief["revision"])
    content = ProductBriefContent.model_validate(latest["content"])
    assert income_by_free_text_is_decided(content), content.model_dump_json(indent=2)
    assert ocr_trade_off_is_named(content), content.model_dump_json(indent=2)

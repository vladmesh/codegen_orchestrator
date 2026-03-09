"""E2E test for PO conversation summarization.

Requires a real LLM API key. Run with:

    PO_LLM_MODEL=anthropic/claude-haiku-4-5 \
    PO_LLM_BASE_URL=https://openrouter.ai/api/v1 \
    PO_LLM_API_KEY=$OPENROUTER_API_KEY \
    pytest services/langgraph/tests/e2e/test_summarization.py -v -s
"""

from __future__ import annotations

import os

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent
import pytest

from src.agents.po.graph import POState, _create_summarization_hook

# Very low thresholds to force summarization within a few messages
SUMMARIZATION_MAX_TOKENS = 500
SUMMARIZATION_TRIGGER_TOKENS = 600
SUMMARIZATION_MAX_SUMMARY_TOKENS = 200

SYSTEM_PROMPT = "You are a helpful assistant. Answer briefly."


def _get_llm_config() -> dict:
    """Get LLM config from env vars, skip if not available."""
    model = os.getenv("PO_LLM_MODEL")
    base_url = os.getenv("PO_LLM_BASE_URL")
    api_key = os.getenv("PO_LLM_API_KEY")

    if not all([model, base_url, api_key]):
        pytest.skip("PO_LLM_MODEL, PO_LLM_BASE_URL, PO_LLM_API_KEY required for E2E tests")

    return {"model": model, "base_url": base_url, "api_key": api_key}


@pytest.mark.asyncio
async def test_summarization_triggers_and_preserves_context():
    """Verify that SummarizationNode triggers on token overflow and preserves key facts.

    Uses a minimal graph with no tools (avoids API/Redis dependencies).
    Only tests summarization mechanics with a real LLM.

    1. Create graph with low token thresholds (MemorySaver, no PostgreSQL)
    2. Send 5 messages that exceed trigger threshold
    3. Verify summarization triggered (running_summary in state context)
    4. Send a question referencing early messages
    5. Verify LLM can answer using summarized context
    """
    llm_config = _get_llm_config()

    llm = ChatOpenAI(
        model=llm_config["model"],
        base_url=llm_config["base_url"],
        api_key=llm_config["api_key"],
    )

    summarization_hook = _create_summarization_hook(
        llm=llm,
        summarization_model=None,
        base_url=llm_config["base_url"],
        api_key=llm_config["api_key"],
        max_tokens=SUMMARIZATION_MAX_TOKENS,
        trigger_tokens=SUMMARIZATION_TRIGGER_TOKENS,
        max_summary_tokens=SUMMARIZATION_MAX_SUMMARY_TOKENS,
    )

    graph = create_react_agent(
        model=llm,
        tools=[],
        prompt=SYSTEM_PROMPT,
        pre_model_hook=summarization_hook,
        state_schema=POState,
        checkpointer=MemorySaver(),
    )

    config = {"configurable": {"thread_id": "test-summarization"}}

    # Send multiple messages to exceed token threshold
    messages = [
        "My project is called AlphaService, it's a REST API for inventory management",
        "We're using PostgreSQL and Redis, the main domain is products",
        "The bot token is stored in TELEGRAM_BOT_TOKEN secret",
        "We decided to use FastAPI with SQLAlchemy for the backend",
        "The project should have a notifications module enabled",
    ]

    for text in messages:
        await graph.ainvoke({"messages": [HumanMessage(content=text)]}, config=config)

    # Check state for running summary
    state = await graph.aget_state(config)
    context = state.values.get("context", {})
    running_summary = context.get("running_summary")

    # Summarization should have triggered given the low thresholds
    assert running_summary is not None, (
        f"Expected running_summary in state context after {len(messages)} messages "
        f"with trigger_tokens={SUMMARIZATION_TRIGGER_TOKENS}. Context: {context}"
    )

    # The summary should contain key facts from the conversation
    summary_text = str(running_summary)
    assert "AlphaService" in summary_text or "inventory" in summary_text, (
        f"Expected summary to contain project facts. Got: {summary_text}"
    )

    # Send a question that requires knowledge from early messages
    result = await graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    content="What's the name of my project and what database are we using?"
                )
            ]
        },
        config=config,
    )

    response = result["messages"][-1].content
    assert "AlphaService" in response, f"Expected 'AlphaService' in response. Got: {response}"
    assert "PostgreSQL" in response or "Postgres" in response, (
        f"Expected 'PostgreSQL' in response. Got: {response}"
    )

"""Opt-in smoke: the configured PO and architect models answer a tool-bound request.

Requires real LLM API keys; each agent skips on its own without its env group:

    PO_LLM_MODEL=openai/gpt-5.6-sol \
    PO_LLM_BASE_URL=https://openrouter.ai/api/v1 \
    PO_LLM_API_KEY=$OPENROUTER_API_KEY \
    ARCHITECT_LLM_MODEL=openai/gpt-5.6-sol \
    ARCHITECT_LLM_BASE_URL=https://openrouter.ai/api/v1 \
    ARCHITECT_LLM_API_KEY=$OPENROUTER_API_KEY \
    pytest services/langgraph/tests/e2e/test_agent_llm_smoke.py -v -s

The model objects are the ones `create_po_graph` and `create_architect_graph`
build; only the graph around them is not run, so no tool reaches a real API.
An unsupported parameter surfaces here as the provider's 400.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
import pytest

from src.agents.architect.graph import create_architect_graph
from src.agents.po.graph import create_po_graph
from src.config.agent_llm_env import AGENT_LLM_ENV


@tool
def get_story(story_id: str) -> str:
    """Return the story text for a story id."""
    return f"Story {story_id}: add a /start command."


def _llm_config(agent: str) -> dict:
    names = AGENT_LLM_ENV[agent]
    values = [os.getenv(name) for name in names]
    if not all(values):
        pytest.skip(f"{', '.join(names)} required for E2E tests")
    return dict(zip(("model", "base_url", "api_key"), values, strict=True))


async def _assert_answers_with_tool_bound(llm) -> None:
    response = await llm.bind_tools([get_story]).ainvoke(
        [HumanMessage(content="Call get_story for story-1, or reply 'ok' if you cannot.")]
    )
    assert isinstance(response, AIMessage)
    assert response.content or response.tool_calls


@pytest.mark.asyncio
async def test_po_model_and_summarizer_answer():
    config = _llm_config("po")
    with (
        patch("src.agents.po.graph.get_all_tools", return_value=[]),
        patch("src.agents.po.graph.create_react_agent", return_value=MagicMock()) as create_agent,
    ):
        await create_po_graph(**config, summarization_model=os.getenv("SUMMARIZATION_MODEL"))
    built = create_agent.call_args.kwargs

    await _assert_answers_with_tool_bound(built["model"])
    summary = await built["pre_model_hook"].model.ainvoke([HumanMessage(content="Say 'ok'.")])
    assert isinstance(summary, AIMessage)


@pytest.mark.asyncio
async def test_architect_model_answers():
    config = _llm_config("architect")
    with patch(
        "src.agents.architect.graph.create_react_agent", return_value=MagicMock()
    ) as create_agent:
        create_architect_graph(**config)

    await _assert_answers_with_tool_bound(create_agent.call_args.kwargs["model"])

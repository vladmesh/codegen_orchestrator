"""What the PO and architect actually send to OpenRouter for `openai/gpt-5.6-sol`.

`langchain-openai` keys its gpt-5 parameter rules on `model.startswith("gpt-5")`,
so the vendor-prefixed OpenRouter id gets none of them: whatever the payload
carries is what OpenRouter receives. These tests run the graphs the consumers
build, on a channel chain whose one channel is openrouter — the real `ChatOpenAI`
— and capture the HTTP request bodies at the transport (no network).

The allowed keys are OpenRouter's `supported_parameters` for the model
(`GET https://openrouter.ai/api/v1/models`, 2026-09-15): no `temperature`,
`top_p`, `top_k` or `stop`; the token limit is `max_completion_tokens`, the name
OpenAI's Chat Completions API uses for reasoning models (`max_tokens` is
deprecated there and rejected by reasoning models).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from langchain_core.messages import HumanMessage
import pytest
import respx

from shared.contracts.dto.llm_channel import LLMChannel, LLMChannelConfig
from src.agents.architect.graph import create_architect_graph
from src.agents.po.graph import create_po_graph
from src.llm import LLMAgent, build_agent_llm

MODEL = "openai/gpt-5.6-sol"
BASE_URL = "https://openrouter.test/api/v1"
MAX_SUMMARY_TOKENS = 64

#: OpenRouter `supported_parameters` of `openai/gpt-5.6-sol`, plus the request envelope.
OPENROUTER_ACCEPTS = {
    "model",
    "messages",
    "stream",
    "include_reasoning",
    "max_completion_tokens",
    "max_tokens",
    "reasoning",
    "reasoning_effort",
    "response_format",
    "seed",
    "structured_outputs",
    "tool_choice",
    "tools",
}
SAMPLING = {
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "top_a",
    "seed",
    "frequency_penalty",
    "presence_penalty",
    "repetition_penalty",
    "logit_bias",
    "logprobs",
    "top_logprobs",
    "n",
    "stop",
}


def _completion(content: str) -> dict:
    return {
        "id": "gen-1",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@pytest.fixture
def openrouter():
    """Record every chat-completions body; answer each with a plain assistant message."""
    bodies: list[dict] = []

    def reply(request):
        bodies.append(json.loads(request.content))
        return respx.MockResponse(200, json=_completion("Understood."))

    with respx.mock(assert_all_called=False) as router:
        router.post(f"{BASE_URL}/chat/completions").mock(side_effect=reply)
        yield bodies


_OPENROUTER_ONLY = [LLMChannelConfig(channel=LLMChannel.OPENROUTER)]


def _settings(summarization_model: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        architect_llm_model=MODEL,
        architect_llm_base_url=BASE_URL,
        architect_llm_api_key="test-key",
        po_llm_model=MODEL,
        po_llm_base_url=BASE_URL,
        po_llm_api_key="test-key",
        summarization_model=summarization_model,
    )


def _assert_accepted_by_openrouter(body: dict) -> None:
    assert body["model"] == MODEL
    assert not SAMPLING & body.keys(), body.keys()
    assert body.keys() <= OPENROUTER_ACCEPTS, body.keys() - OPENROUTER_ACCEPTS
    assert "max_tokens" not in body


@pytest.mark.asyncio
async def test_the_architect_request_carries_tools_and_no_sampling_parameter(openrouter):
    graph = create_architect_graph(
        build_agent_llm(LLMAgent.ARCHITECT, _OPENROUTER_ONLY, _settings())
    )

    await graph.ainvoke(
        {
            "messages": [HumanMessage(content="Decompose story story-abc.")],
            "story_id": "story-abc",
            "project_id": "proj-1",
            "telegram_chat_id": "chat-1",
            "product_brief_id": "brief-1",
            "planning_attempt_id": "plan-1",
            "must_requirements": [],
        }
    )

    (body,) = openrouter
    _assert_accepted_by_openrouter(body)
    assert body["tools"]
    assert "max_completion_tokens" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("summarization_model", [None, MODEL], ids=["reuses-main", "separate"])
async def test_po_main_and_summarizer_requests_carry_no_sampling_parameter(
    openrouter, summarization_model
):
    settings = _settings(summarization_model)
    graph = await create_po_graph(
        llm=build_agent_llm(LLMAgent.PO, _OPENROUTER_ONLY, settings),
        summarization_llm=build_agent_llm(LLMAgent.PO_SUMMARIZER, _OPENROUTER_ONLY, settings),
        summarization_max_tokens=256,
        summarization_trigger_tokens=128,
        summarization_max_summary_tokens=MAX_SUMMARY_TOKENS,
    )
    config = {"configurable": {"thread_id": "po-payload", "telegram_chat_id": "42"}}
    long_turn = "I want a bot that tracks my expenses and incomes. " * 40

    for text in (long_turn, long_turn, "What did I ask for?"):
        await graph.ainvoke({"messages": [HumanMessage(content=text)]}, config=config)

    main = [body for body in openrouter if "tools" in body]
    summaries = [body for body in openrouter if "tools" not in body]
    assert main, "the PO never called its model"
    assert summaries, "the conversation never reached the summarizer"
    for body in main:
        _assert_accepted_by_openrouter(body)
        assert "max_completion_tokens" not in body
    for body in summaries:
        _assert_accepted_by_openrouter(body)
        assert body["max_completion_tokens"] == MAX_SUMMARY_TOKENS

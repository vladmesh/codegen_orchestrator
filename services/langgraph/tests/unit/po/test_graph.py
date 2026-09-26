"""Unit tests for PO graph."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from langgraph.prebuilt.chat_agent_executor import AgentState
from langmem.short_term import SummarizationNode
import pytest

from shared.contracts.dto.llm_channel import LLMChannel, default_llm_channel_chain
from src.agents.po.graph import (
    POState,
    _create_summarization_hook,
    create_po_graph,
)
from src.llm import LLMAgent, build_agent_llm
from src.prompts.po import SYSTEM_PROMPT


class TestPOState:
    def test_po_state_has_context_field(self):
        annotations = POState.__annotations__
        assert "context" in annotations

    def test_po_state_extends_agent_state(self):
        # TypedDict uses __orig_bases__ for inheritance tracking
        assert AgentState in getattr(POState, "__orig_bases__", ())


def _settings(summarization_model: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        po_llm_model="test-model",
        po_llm_base_url="https://example.com/v1",
        po_llm_api_key="test-key",
        summarization_model=summarization_model,
        llm_codex_home=None,
        claude_code_oauth_token=None,
    )


def _summarizer(summarization_model: str | None = None):
    return build_agent_llm(
        LLMAgent.PO_SUMMARIZER, default_llm_channel_chain(), _settings(summarization_model)
    )


def _openrouter_model_name(chain) -> str:
    [slot] = [slot for slot in chain.slots if slot.channel is LLMChannel.OPENROUTER]
    return slot.chat_model.model_name


class TestCreateSummarizationHook:
    def test_creates_summarization_node(self):
        hook = _create_summarization_hook(
            summarization_llm=_summarizer(),
            max_tokens=50_000,
            trigger_tokens=60_000,
            max_summary_tokens=2_000,
        )
        assert isinstance(hook, SummarizationNode)

    def test_uses_separate_model_when_configured(self):
        summarizer = _summarizer("cheap-model")
        hook = _create_summarization_hook(
            summarization_llm=summarizer,
            max_tokens=50_000,
            trigger_tokens=60_000,
            max_summary_tokens=2_000,
        )
        assert isinstance(hook, SummarizationNode)
        # The hook's model is the summarizer's channel chain bound with max_tokens
        # (a RunnableBinding); its openrouter channel is the cheap model.
        bound_model = hook.model
        assert bound_model.kwargs.get("max_tokens") == 2_000  # noqa: PLR2004
        assert bound_model.bound is summarizer
        assert _openrouter_model_name(bound_model.bound) == "cheap-model"

    def test_falls_back_to_main_model(self):
        hook = _create_summarization_hook(
            summarization_llm=_summarizer(None),
            max_tokens=50_000,
            trigger_tokens=60_000,
            max_summary_tokens=2_000,
        )
        # Without SUMMARIZATION_MODEL the summarizer's openrouter channel is the PO's model
        bound_model = hook.model
        assert bound_model.kwargs.get("max_tokens") == 2_000  # noqa: PLR2004
        assert _openrouter_model_name(bound_model.bound) == "test-model"

    def test_respects_token_parameters(self):
        hook = _create_summarization_hook(
            summarization_llm=_summarizer(),
            max_tokens=10_000,
            trigger_tokens=15_000,
            max_summary_tokens=500,
        )
        assert hook.max_tokens == 10_000  # noqa: PLR2004
        assert hook.max_tokens_before_summary == 15_000  # noqa: PLR2004
        assert hook.max_summary_tokens == 500  # noqa: PLR2004

    def test_output_key_is_llm_input_messages(self):
        hook = _create_summarization_hook(
            summarization_llm=_summarizer(),
            max_tokens=50_000,
            trigger_tokens=60_000,
            max_summary_tokens=2_000,
        )
        assert hook.output_messages_key == "llm_input_messages"


class TestCreatePOGraph:
    @pytest.mark.asyncio
    @patch("src.agents.po.graph.get_all_tools", return_value=[])
    @patch("src.agents.po.graph.create_react_agent")
    async def test_creates_graph_with_summarization(self, mock_create_agent, mock_tools):
        mock_create_agent.return_value = MagicMock()
        llm, summarizer = MagicMock(), _summarizer()

        await create_po_graph(llm=llm, summarization_llm=summarizer)

        mock_create_agent.assert_called_once()
        call_kwargs = mock_create_agent.call_args[1]
        assert call_kwargs["model"] is llm
        assert call_kwargs["prompt"] == SYSTEM_PROMPT
        assert isinstance(call_kwargs["pre_model_hook"], SummarizationNode)
        assert call_kwargs["pre_model_hook"].model.bound is summarizer
        assert call_kwargs["state_schema"] is POState

    @pytest.mark.asyncio
    @patch("src.agents.po.graph.get_all_tools", return_value=[])
    @patch("src.agents.po.graph.create_react_agent")
    async def test_creates_graph_with_memory_saver_fallback(self, mock_create_agent, mock_tools):
        from langgraph.checkpoint.memory import MemorySaver

        mock_create_agent.return_value = MagicMock()

        await create_po_graph(
            llm=MagicMock(),
            summarization_llm=_summarizer(),
            checkpoint_database_url=None,
        )

        call_kwargs = mock_create_agent.call_args[1]
        assert isinstance(call_kwargs["checkpointer"], MemorySaver)

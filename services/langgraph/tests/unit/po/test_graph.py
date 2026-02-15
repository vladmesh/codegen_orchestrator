"""Unit tests for PO graph."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from langchain_openai import ChatOpenAI
from langgraph.prebuilt.chat_agent_executor import AgentState
from langmem.short_term import SummarizationNode
import pytest

from src.po.graph import (
    POState,
    _create_summarization_hook,
    create_po_graph,
)
from src.po.prompts import SYSTEM_PROMPT


class TestPOState:
    def test_po_state_has_context_field(self):
        annotations = POState.__annotations__
        assert "context" in annotations

    def test_po_state_extends_agent_state(self):
        # TypedDict uses __orig_bases__ for inheritance tracking
        assert AgentState in getattr(POState, "__orig_bases__", ())


class TestCreateSummarizationHook:
    def setup_method(self):
        self.llm = ChatOpenAI(
            model="test-model",
            base_url="https://example.com/v1",
            api_key="test-key",
        )

    def test_creates_summarization_node(self):
        hook = _create_summarization_hook(
            llm=self.llm,
            summarization_model=None,
            base_url="https://example.com/v1",
            api_key="test-key",
            max_tokens=50_000,
            trigger_tokens=60_000,
            max_summary_tokens=2_000,
        )
        assert isinstance(hook, SummarizationNode)

    def test_uses_separate_model_when_configured(self):
        hook = _create_summarization_hook(
            llm=self.llm,
            summarization_model="cheap-model",
            base_url="https://example.com/v1",
            api_key="test-key",
            max_tokens=50_000,
            trigger_tokens=60_000,
            max_summary_tokens=2_000,
        )
        assert isinstance(hook, SummarizationNode)
        # The hook's model should be bound with max_tokens (a RunnableBinding)
        # and the underlying model should be the cheap model
        bound_model = hook.model
        assert bound_model.kwargs.get("max_tokens") == 2_000  # noqa: PLR2004
        assert bound_model.bound.model_name == "cheap-model"

    def test_falls_back_to_main_model(self):
        hook = _create_summarization_hook(
            llm=self.llm,
            summarization_model=None,
            base_url="https://example.com/v1",
            api_key="test-key",
            max_tokens=50_000,
            trigger_tokens=60_000,
            max_summary_tokens=2_000,
        )
        # Should use the main LLM bound with max_tokens
        bound_model = hook.model
        assert bound_model.kwargs.get("max_tokens") == 2_000  # noqa: PLR2004
        assert bound_model.bound.model_name == "test-model"

    def test_respects_token_parameters(self):
        hook = _create_summarization_hook(
            llm=self.llm,
            summarization_model=None,
            base_url="https://example.com/v1",
            api_key="test-key",
            max_tokens=10_000,
            trigger_tokens=15_000,
            max_summary_tokens=500,
        )
        assert hook.max_tokens == 10_000  # noqa: PLR2004
        assert hook.max_tokens_before_summary == 15_000  # noqa: PLR2004
        assert hook.max_summary_tokens == 500  # noqa: PLR2004

    def test_output_key_is_llm_input_messages(self):
        hook = _create_summarization_hook(
            llm=self.llm,
            summarization_model=None,
            base_url="https://example.com/v1",
            api_key="test-key",
            max_tokens=50_000,
            trigger_tokens=60_000,
            max_summary_tokens=2_000,
        )
        assert hook.output_messages_key == "llm_input_messages"


class TestCreatePOGraph:
    @pytest.mark.asyncio
    @patch("src.po.graph.get_all_tools", return_value=[])
    @patch("src.po.graph.create_react_agent")
    async def test_creates_graph_with_summarization(self, mock_create_agent, mock_tools):
        mock_create_agent.return_value = MagicMock()

        await create_po_graph(
            model="test-model",
            base_url="https://example.com/v1",
            api_key="test-key",
        )

        mock_create_agent.assert_called_once()
        call_kwargs = mock_create_agent.call_args[1]
        assert call_kwargs["prompt"] == SYSTEM_PROMPT
        assert isinstance(call_kwargs["pre_model_hook"], SummarizationNode)
        assert call_kwargs["state_schema"] is POState

    @pytest.mark.asyncio
    @patch("src.po.graph.get_all_tools", return_value=[])
    @patch("src.po.graph.create_react_agent")
    async def test_creates_graph_with_memory_saver_fallback(self, mock_create_agent, mock_tools):
        from langgraph.checkpoint.memory import MemorySaver

        mock_create_agent.return_value = MagicMock()

        await create_po_graph(
            model="test-model",
            base_url="https://example.com/v1",
            api_key="test-key",
            checkpoint_database_url=None,
        )

        call_kwargs = mock_create_agent.call_args[1]
        assert isinstance(call_kwargs["checkpointer"], MemorySaver)

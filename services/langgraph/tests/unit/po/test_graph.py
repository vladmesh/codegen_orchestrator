"""Unit tests for PO graph."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fakeredis.aioredis import FakeRedis
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.prebuilt.chat_agent_executor import AgentState
from langmem.short_term import SummarizationNode
from pydantic import Field
import pytest

from shared.contracts.dto.llm_channel import LLMChannel, default_llm_channel_chain
from shared.contracts.queues.po import POSystemEvent, POUserMessage, po_thread_id
from shared.contracts.vocab import OwnerNotificationEvent
from src.agents.po.graph import (
    POState,
    _create_summarization_hook,
    create_po_graph,
    po_prompt,
)
from src.agents.po.situation import (
    DEFERRED_NOTICES_HEADING,
    SITUATION_CONFIG_KEY,
    SNAPSHOT_HEADING,
)
from src.consumers.po import _handle_message
from src.llm import LLMAgent, build_agent_llm
from src.prompts.po import SYSTEM_PROMPT

CHAT = "1015926438"


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
        assert call_kwargs["prompt"] is po_prompt
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


class _RecordingModel(BaseChatModel):
    """Answers every turn with one line and keeps what it was given."""

    inputs: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "recording"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003 - test stand-in
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ANN003
        self.inputs.append(list(messages))
        reply = AIMessage(content="Work on your order is stopped; a person is needed.")
        return ChatResult(generations=[ChatGeneration(message=reply)])


class TestPOPrompt:
    def test_a_run_without_a_snapshot_gets_the_system_prompt_alone(self):
        human = HumanMessage(content="hi")
        messages = po_prompt({"messages": [human]}, {"configurable": {}})
        assert messages == [SystemMessage(content=SYSTEM_PROMPT), human]

    def test_a_run_with_a_snapshot_gets_it_after_the_system_prompt(self):
        human = HumanMessage(content="[system: system_event:story_blocked] stopped")
        snapshot = f"{SNAPSHOT_HEADING} (built for this system event)\n- Story: story-1"
        messages = po_prompt(
            {"messages": [human]}, {"configurable": {SITUATION_CONFIG_KEY: snapshot}}
        )
        assert messages == [SystemMessage(content=f"{SYSTEM_PROMPT}\n\n{snapshot}"), human]


class TestTheSnapshotIsNotStored:
    """The model reads the snapshot; the checkpointed thread never holds it."""

    @pytest.fixture
    async def po(self):
        model = _RecordingModel()
        graph = await create_po_graph(
            llm=model, summarization_llm=model, checkpoint_database_url=None
        )
        return graph, model

    @pytest.fixture
    def client(self):
        client = AsyncMock()
        client.redis = FakeRedis(decode_responses=True)
        client.publish_flat = AsyncMock()
        return client

    async def test_a_system_event_turn_saves_the_event_but_not_its_snapshot(self, po, client):
        graph, model = po
        event = POSystemEvent(
            event=OwnerNotificationEvent.STORY_BLOCKED,
            text="Work on the story is stopped.",
            story_id="story-order",
            project_id="00000000-0000-0000-0000-000000000001",
            telegram_chat_id=CHAT,
        ).model_dump(mode="json")

        await _handle_message(graph, client, CHAT, event)

        [model_input] = model.inputs
        assert isinstance(model_input[0], SystemMessage)
        assert SNAPSHOT_HEADING in model_input[0].content
        assert DEFERRED_NOTICES_HEADING in model_input[0].content

        saved = await graph.aget_state({"configurable": {"thread_id": po_thread_id(CHAT)}})
        contents = [str(message.content) for message in saved.values["messages"]]
        assert any("system_event:story_blocked" in text for text in contents)
        assert not any(SNAPSHOT_HEADING in text for text in contents)
        assert not any(DEFERRED_NOTICES_HEADING in text for text in contents)

        # The next user turn reads the thread back without any snapshot in it.
        await _handle_message(
            graph,
            client,
            CHAT,
            POUserMessage(
                text="How is my bot going?", telegram_chat_id=CHAT, request_id="req-1"
            ).model_dump(mode="json"),
        )
        user_turn_input = model.inputs[-1]
        assert user_turn_input[0] == SystemMessage(content=SYSTEM_PROMPT)
        assert not any(SNAPSHOT_HEADING in str(message.content) for message in user_turn_input)

"""Unit tests for architect graph creation."""

from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
import pytest


class TestCreateArchitectGraph:
    @patch("src.agents.architect.graph.ChatOpenAI")
    def test_graph_compiles(self, mock_chat):
        mock_chat.return_value = MagicMock()
        from src.agents.architect.graph import create_architect_graph

        graph = create_architect_graph(
            model="test-model",
            base_url="http://localhost:1234",
            api_key="test-key",
        )

        assert graph is not None
        # Graph should have nodes
        assert len(graph.nodes) > 0

    @patch("src.agents.architect.graph.ChatOpenAI")
    def test_graph_has_agent_node(self, mock_chat):
        mock_chat.return_value = MagicMock()
        from src.agents.architect.graph import create_architect_graph

        graph = create_architect_graph(
            model="test-model",
            base_url="http://localhost:1234",
            api_key="test-key",
        )

        node_names = set(graph.nodes.keys())
        assert "agent" in node_names
        assert "tools" in node_names


class TestArchitectPrompt:
    def test_prompt_is_nonempty(self):
        from src.prompts.architect import SYSTEM_PROMPT

        assert len(SYSTEM_PROMPT) > 100

    def test_prompt_contains_key_instructions(self):
        from src.prompts.architect import SYSTEM_PROMPT

        assert "create_task" in SYSTEM_PROMPT
        assert "get_story" in SYSTEM_PROMPT
        assert "automatically chained" in SYSTEM_PROMPT
        assert "acceptance_criteria" in SYSTEM_PROMPT


class TestArchitectState:
    def test_state_has_required_fields(self):
        from src.agents.architect.state import ArchitectState

        # TypedDict fields
        annotations = ArchitectState.__annotations__
        assert "story_id" in annotations
        assert "project_id" in annotations
        assert "telegram_chat_id" in annotations
        assert "messages" in annotations

    def test_state_carries_the_planning_identity(self):
        """The attempt and the requirements are state, so tools read them injected."""
        from src.agents.architect.state import ArchitectState

        annotations = ArchitectState.__annotations__
        assert "product_brief_id" in annotations
        assert "planning_attempt_id" in annotations
        assert "must_requirements" in annotations


class TestArchitectSettings:
    def test_settings_have_architect_fields(self):
        from src.config.settings import Settings

        fields = Settings.model_fields
        assert "architect_llm_model" in fields
        assert "architect_llm_base_url" in fields
        assert "architect_llm_api_key" in fields

    def test_architect_fields_default_to_none(self):
        """Settings should load without architect env vars set."""
        import os

        with patch.dict(
            os.environ,
            {
                "REDIS_URL": "redis://localhost:6379",
                "API_BASE_URL": "http://localhost:8000",
            },
            clear=False,
        ):
            from src.config.settings import Settings

            s = Settings(_env_file=None)
            assert s.architect_llm_model is None
            assert s.architect_llm_base_url is None
            assert s.architect_llm_api_key is None


class _ScriptedToolCallingModel(BaseChatModel):
    """A model that emits scripted turns, then repeats its last one.

    The point of the test below is the graph, not the model: this stands in for
    the LLM so that a real `create_react_agent` run reaches the real tools.
    `bind_tools` returns the model itself because what the tools declare to the
    model is asserted elsewhere (`tool_call_schema`); here the tool call is
    written by hand, deliberately naming no planning identity.
    """

    turns: list[AIMessage]

    @property
    def _llm_type(self) -> str:
        return "scripted-tool-calling"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003 - test stand-in
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ANN003
        turn = self.turns.pop(0) if len(self.turns) > 1 else self.turns[0]
        return ChatResult(generations=[ChatGeneration(message=turn)])


class TestPlanningIdentityIsInjectedIntoTools:
    """The whole brief-backed feature rests on `InjectedState` actually injecting.

    Every other test hands the tools their planning identity explicitly. If
    injection did not work, `create_task` would send no attempt id and the API
    would refuse every brief-backed task with 409 — a total failure whose only
    cheap place to find out is here, so this drives the graph the consumer
    builds and asserts what came out the far end.
    """

    @pytest.fixture(autouse=True)
    def _reset_chain(self):
        from src.agents.architect.tools import reset_task_chain

        reset_task_chain()
        yield
        reset_task_chain()

    @pytest.mark.asyncio
    async def test_create_task_carries_the_attempt_the_model_never_saw(self):
        from tests.unit.factories import make_task

        tool_call = {
            "name": "create_task",
            # Exactly what a model can say: no attempt id, no brief id.
            "args": {
                "title": "Add User model",
                "description": "Create model",
                "type": "feature",
                "acceptance_criteria": "Model exists",
                "story_id": "story-abc",
                "project_id": "proj-1",
            },
            "id": "call-1",
        }
        model = _ScriptedToolCallingModel(
            turns=[
                AIMessage(content="", tool_calls=[tool_call]),
                AIMessage(content="done"),
            ]
        )

        with (
            patch("src.agents.architect.graph.ChatOpenAI", return_value=model),
            patch("src.agents.architect.tools.api_client") as api,
        ):
            api.create_task = AsyncMock(return_value=make_task(id="task-new", title="New task"))
            from src.agents.architect.graph import create_architect_graph

            graph = create_architect_graph(
                model="test-model",
                base_url="http://localhost:1234",
                api_key="test-key",
            )
            await graph.ainvoke(
                {
                    "messages": [{"role": "user", "content": "Decompose story story-abc."}],
                    "story_id": "story-abc",
                    "project_id": "proj-1",
                    "telegram_chat_id": "chat-1",
                    "product_brief_id": "brief-1",
                    "planning_attempt_id": "plan-1",
                    "must_requirements": [],
                }
            )

        payload = api.create_task.call_args[0][0]
        assert payload["planning_attempt_id"] == "plan-1"

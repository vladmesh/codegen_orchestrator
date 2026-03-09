"""Unit tests for architect graph creation."""

from unittest.mock import MagicMock, patch


class TestCreateArchitectGraph:
    @patch("src.architect.graph.ChatOpenAI")
    def test_graph_compiles(self, mock_chat):
        mock_chat.return_value = MagicMock()
        from src.architect.graph import create_architect_graph

        graph = create_architect_graph(
            model="test-model",
            base_url="http://localhost:1234",
            api_key="test-key",
        )

        assert graph is not None
        # Graph should have nodes
        assert len(graph.nodes) > 0

    @patch("src.architect.graph.ChatOpenAI")
    def test_graph_has_agent_node(self, mock_chat):
        mock_chat.return_value = MagicMock()
        from src.architect.graph import create_architect_graph

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
        assert "blocked_by_task_id" in SYSTEM_PROMPT
        assert "acceptance_criteria" in SYSTEM_PROMPT


class TestArchitectState:
    def test_state_has_required_fields(self):
        from src.architect.state import ArchitectState

        # TypedDict fields
        annotations = ArchitectState.__annotations__
        assert "story_id" in annotations
        assert "project_id" in annotations
        assert "user_id" in annotations
        assert "messages" in annotations


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

            s = Settings()
            assert s.architect_llm_model is None
            assert s.architect_llm_base_url is None
            assert s.architect_llm_api_key is None

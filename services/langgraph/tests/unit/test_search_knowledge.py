"""Unit tests for search_knowledge base tool.

Tests RAG search with different scopes.
"""

from unittest.mock import AsyncMock, patch

from src.capabilities.base import search_knowledge
from src.state.context import set_tool_context


class TestSearchKnowledge:
    """Test search_knowledge tool with different scopes."""

    async def test_search_history(self):
        """Test searching conversation history."""
        # Set tool context
        state = {"user_id": 42, "current_project": "test_project"}
        set_tool_context(state)

        # Mock API response
        with patch("src.clients.api.api_client") as mock_api_client:
            mock_api_client.get = AsyncMock(
                return_value=[
                    {
                        "summary_text": "User created project hello-world",
                        "created_at": "2024-01-15T10:00:00Z",
                        "similarity": 0.95,
                    }
                ]
            )

            result = await search_knowledge.ainvoke({"query": "hello world", "scope": "history"})

            assert "results" in result
            assert len(result["results"]) == 1  # noqa: PLR2004
            assert result["results"][0]["source"] == "history"
            assert "hello-world" in result["results"][0]["content"]

    async def test_search_docs(self):
        """Test searching project documentation."""
        state = {"user_id": 42, "current_project": "test_project"}
        set_tool_context(state)

        # Mock RAG search response
        with patch("src.tools.rag.search_project_context") as mock_search:
            mock_search.ainvoke = AsyncMock(
                return_value={
                    "results": [
                        {
                            "content": "README documentation",
                            "relevance": 0.9,
                            "file_path": "README.md",
                        }
                    ]
                }
            )

            result = await search_knowledge.ainvoke({"query": "readme", "scope": "docs"})

            assert "results" in result
            assert len(result["results"]) == 1
            assert result["results"][0]["source"] == "docs"
            assert "README" in result["results"][0]["content"]

    async def test_search_logs(self):
        """Test searching service logs."""
        state = {"user_id": 42, "current_project": "test_project"}
        set_tool_context(state)

        # Mock error history
        with patch("src.tools.diagnose.get_error_history") as mock_get_errors:
            mock_get_errors.ainvoke = AsyncMock(
                return_value={
                    "errors": [
                        {"message": "Connection timeout", "count": 5},
                        {"message": "404 Not Found", "count": 2},
                    ]
                }
            )

            result = await search_knowledge.ainvoke({"query": "error", "scope": "logs"})

            assert "results" in result
            assert len(result["results"]) >= 1  # noqa: PLR2004
            assert result["results"][0]["source"] == "logs"
            assert "Connection timeout" in result["results"][0]["content"]

    async def test_search_all_scopes(self):
        """Test searching all scopes."""
        state = {"user_id": 42, "current_project": "test_project"}
        set_tool_context(state)

        with (
            patch("src.clients.api.api_client") as mock_api,
            patch("src.tools.rag.search_project_context") as mock_docs,
            patch("src.tools.diagnose.get_error_history") as mock_logs,
        ):
            # Mock all sources
            mock_api.get = AsyncMock(
                return_value=[{"summary_text": "History result", "similarity": 0.9}]
            )
            mock_docs.ainvoke = AsyncMock(
                return_value={"results": [{"content": "Docs result", "relevance": 0.85}]}
            )
            mock_logs.ainvoke = AsyncMock(
                return_value={"errors": [{"message": "Log error", "count": 3}]}
            )

            result = await search_knowledge.ainvoke({"query": "test", "scope": "all"})

            assert "results" in result
            # Should have results from multiple sources
            sources = {r["source"] for r in result["results"]}
            assert "history" in sources
            assert "docs" in sources
            assert "logs" in sources

    async def test_invalid_scope(self):
        """Test error handling for invalid scope."""
        state = {"user_id": 42, "current_project": "test_project"}
        set_tool_context(state)

        result = await search_knowledge.ainvoke({"query": "test", "scope": "invalid"})

        assert "error" in result
        assert "Unknown scope" in result["error"]

    async def test_no_user_id_history_scope(self):
        """Test searching history without user_id."""
        state = {"user_id": None, "current_project": "test_project"}
        set_tool_context(state)

        result = await search_knowledge.ainvoke({"query": "test", "scope": "history"})

        # Should return empty results
        assert result["results"] == []

    async def test_no_project_docs_scope(self):
        """Test searching docs without project."""
        state = {"user_id": 42, "current_project": None}
        set_tool_context(state)

        result = await search_knowledge.ainvoke({"query": "test", "scope": "docs"})

        # Should return empty results
        assert result["results"] == []

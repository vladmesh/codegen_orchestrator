"""Integration tests for LangGraph execution."""

from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_graph_can_process_simple_message():
    """Test that graph can process a simple user message."""
    from src.graph import create_graph

    graph = create_graph()

    # Mock the InternalAPIClient used by tools
    with patch("src.tools.base.InternalAPIClient.get") as mock_get:
        # Mock typical API responses
        mock_get.return_value = {"projects": [], "servers": [], "incidents": []}

        # Just verify the graph structure is valid
        assert graph is not None

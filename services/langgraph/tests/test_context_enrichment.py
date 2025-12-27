import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import the internal function for testing
# We need to make sure the path is correct relative to where pytest is run
# or modify sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../src")))

from src.worker import _get_conversation_context

TEST_USER_ID = 123


@pytest.mark.asyncio
async def test_get_conversation_context_returns_summaries():
    with patch("src.worker.api_client") as mock_client:
        # Mock successful response
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"summary_text": "Summary 1"},
            {"summary_text": "Summary 2"},
        ]
        mock_client.get = AsyncMock(return_value=mock_response)

        context = await _get_conversation_context(user_id=TEST_USER_ID)

        assert "Summary 1" in context
        assert "Summary 2" in context
        assert "Summary 1\n\nSummary 2" == context


@pytest.mark.asyncio
async def test_get_conversation_context_returns_none_on_error():
    with patch("src.worker.api_client") as mock_client:
        mock_client.get = AsyncMock(side_effect=Exception("API Error"))

        context = await _get_conversation_context(user_id=TEST_USER_ID)
        assert context is None

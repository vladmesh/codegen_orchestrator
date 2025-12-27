from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.models import RAGConversationSummary
from src.routers.rag import get_summaries

TEST_USER_ID = 123
NON_EXISTENT_USER_ID = 999


@pytest.mark.asyncio
async def test_get_summaries():
    # Mock DB session
    mock_db = AsyncMock()

    # Mock result
    mock_summary = RAGConversationSummary(
        id=1,
        user_id=TEST_USER_ID,
        summary_text="Test summary",
        created_at=datetime.utcnow(),
        message_ids=[],
        project_id="p1",
        thread_id="t1",
    )

    # Mock execute result
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_summary]
    mock_db.execute.return_value = mock_result

    summaries = await get_summaries(user_id=TEST_USER_ID, limit=5, db=mock_db)

    assert len(summaries) == 1
    assert summaries[0].summary_text == "Test summary"
    assert summaries[0].user_id == TEST_USER_ID


@pytest.mark.asyncio
async def test_get_summaries_empty():
    # Mock DB session
    mock_db = AsyncMock()

    # Mock execute result
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_db.execute.return_value = mock_result

    summaries = await get_summaries(user_id=NON_EXISTENT_USER_ID, limit=5, db=mock_db)

    assert len(summaries) == 0

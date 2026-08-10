"""PO refuses to answer a user-facing message it cannot deliver.

Producers resolve the Telegram chat before they publish, so an event that
arrives without one is a defect. PO does not quietly drop it: nothing is
invoked, and admins are told which story and project lost its recipient.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage
import pytest

from shared.contracts.queues.po import po_thread_id
from src.consumers.po import _handle_message


@pytest.fixture
def mock_graph():
    graph = AsyncMock()
    graph.ainvoke.return_value = {"messages": [AIMessage(content="Работа продолжается")]}
    clean_state = AsyncMock()
    clean_state.values = {"messages": []}
    graph.aget_state.return_value = clean_state
    return graph


@pytest.fixture
def mock_client():
    client = AsyncMock()
    client.redis = AsyncMock()
    client.publish_flat = AsyncMock()
    return client


class TestUnaddressableEvent:
    @pytest.mark.asyncio
    async def test_story_event_without_recipient_is_not_invoked(self, mock_graph, mock_client):
        data = {
            "type": "system_event",
            "event": "story_failed",
            "text": "Story failed",
            "story_id": "story-9",
            "project_id": "proj-4",
        }

        with patch("src.consumers.po.notify_admins_best_effort", new=AsyncMock()):
            await _handle_message(mock_graph, mock_client, "", data)

        mock_graph.ainvoke.assert_not_called()
        mock_client.publish_flat.assert_not_called()

    @pytest.mark.asyncio
    async def test_story_event_without_recipient_alerts_admins(self, mock_graph, mock_client):
        data = {
            "type": "system_event",
            "event": "story_failed",
            "text": "Story failed",
            "story_id": "story-9",
            "project_id": "proj-4",
            "owner_user_id": "17",
        }

        with patch("src.consumers.po.notify_admins_best_effort", new=AsyncMock()) as alert:
            await _handle_message(mock_graph, mock_client, "", data)

        alert.assert_awaited_once()
        message = alert.await_args.args[0]
        assert "story-9" in message
        assert "proj-4" in message
        assert "story_failed" in message
        assert "17" in message


class TestOneThreadPerChat:
    @pytest.mark.asyncio
    async def test_user_message_and_pipeline_event_share_a_thread(self, mock_graph, mock_client):
        """AC: whichever producer raised it, it lands in the user's conversation."""
        chat_id = "987654321"

        await _handle_message(
            mock_graph,
            mock_client,
            chat_id,
            {"type": "user_message", "text": "как дела?", "request_id": "req-1"},
        )
        from_user = mock_graph.ainvoke.call_args.kwargs["config"]["configurable"]["thread_id"]

        await _handle_message(
            mock_graph,
            mock_client,
            chat_id,
            {
                "type": "system_event",
                "event": "story_completed",
                "text": "Story completed",
                "story_id": "story-9",
                "project_id": "proj-4",
            },
        )
        from_pipeline = mock_graph.ainvoke.call_args.kwargs["config"]["configurable"]["thread_id"]

        assert from_user == from_pipeline == po_thread_id(chat_id)

    @pytest.mark.asyncio
    async def test_proactive_notification_keeps_the_event_identifiers(
        self, mock_graph, mock_client
    ):
        """The transport needs them to raise a useful alert if delivery fails."""
        await _handle_message(
            mock_graph,
            mock_client,
            "987654321",
            {
                "type": "system_event",
                "event": "story_completed",
                "text": "Story completed",
                "story_id": "story-9",
                "project_id": "proj-4",
                "owner_user_id": "17",
            },
        )

        fields = mock_client.publish_flat.await_args.args[1]
        assert fields["telegram_chat_id"] == "987654321"
        assert fields["story_id"] == "story-9"
        assert fields["project_id"] == "proj-4"
        assert fields["owner_user_id"] == "17"
        assert fields["event"] == "story_completed"

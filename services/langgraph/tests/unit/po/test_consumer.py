"""Unit tests for PO consumer."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from langchain_core.messages import AIMessage, HumanMessage
import pytest

from src.po.consumer import _handle_message, _process_message


@pytest.fixture
def mock_graph():
    """Mock compiled graph with ainvoke."""
    graph = AsyncMock()
    graph.ainvoke.return_value = {"messages": [AIMessage(content="Hello! How can I help?")]}
    return graph


@pytest.fixture
def mock_client():
    """Mock RedisStreamClient."""
    client = AsyncMock()
    client.redis = AsyncMock()
    client.publish_flat = AsyncMock()
    return client


class TestHandleMessage:
    @pytest.mark.asyncio
    async def test_user_message_creates_human_message(self, mock_graph, mock_client):
        data = {
            "type": "user_message",
            "text": "hello",
            "timestamp": "2026-02-15T10:00:00",
            "request_id": "req-1",
        }

        await _handle_message(mock_graph, mock_client, "user-1", data)

        mock_graph.ainvoke.assert_called_once()
        call_args = mock_graph.ainvoke.call_args
        msg = call_args[0][0]["messages"][0]
        assert isinstance(msg, HumanMessage)
        assert "2026-02-15T10:00:00" in msg.content
        assert "hello" in msg.content
        assert "[system:" not in msg.content

    @pytest.mark.asyncio
    async def test_system_event_uses_human_message_with_prefix(self, mock_graph, mock_client):
        data = {
            "type": "system_event",
            "event": "completed",
            "text": "engineering_completed",
            "timestamp": "2026-02-15T10:00:00",
            "request_id": "req-1",
        }

        await _handle_message(mock_graph, mock_client, "user-1", data)

        msg = mock_graph.ainvoke.call_args[0][0]["messages"][0]
        assert isinstance(msg, HumanMessage)
        assert msg.content.startswith("[system: system_event:completed]")
        assert "engineering_completed" in msg.content

    @pytest.mark.asyncio
    async def test_reminder_uses_human_message_with_prefix(self, mock_graph, mock_client):
        data = {
            "type": "reminder",
            "text": "check task eng-123",
            "timestamp": "2026-02-15T10:00:00",
        }

        await _handle_message(mock_graph, mock_client, "user-1", data)

        msg = mock_graph.ainvoke.call_args[0][0]["messages"][0]
        assert isinstance(msg, HumanMessage)
        assert msg.content.startswith("[system: reminder]")
        assert "check task eng-123" in msg.content

    @pytest.mark.asyncio
    async def test_uses_thread_id_per_user(self, mock_graph, mock_client):
        data = {"type": "user_message", "text": "hi", "request_id": "req-1"}

        await _handle_message(mock_graph, mock_client, "user-42", data)

        config = mock_graph.ainvoke.call_args[1]["config"]
        assert config["configurable"]["thread_id"] == "po-user-user-42"

    @pytest.mark.asyncio
    async def test_writes_response_with_request_id(self, mock_graph, mock_client):
        data = {
            "type": "user_message",
            "text": "hello",
            "request_id": "req-123",
        }

        await _handle_message(mock_graph, mock_client, "user-1", data)

        mock_client.publish_flat.assert_called_once()
        call_args = mock_client.publish_flat.call_args
        assert call_args[0][0] == "po:response:req-123"
        assert call_args[0][1]["text"] == "Hello! How can I help?"
        assert call_args[0][1]["user_id"] == "user-1"

    @pytest.mark.asyncio
    async def test_no_request_id_forwards_to_proactive(self, mock_graph, mock_client):
        """Without request_id, non-empty response should go to po:proactive."""
        data = {"type": "system_event", "event": "completed", "text": "scaffolding_done"}

        await _handle_message(mock_graph, mock_client, "user-1", data)

        mock_client.publish_flat.assert_called_once()
        call_args = mock_client.publish_flat.call_args
        assert call_args[0][0] == "po:proactive"
        assert call_args[0][1]["text"] == "Hello! How can I help?"
        assert call_args[0][1]["user_id"] == "user-1"

    @pytest.mark.asyncio
    async def test_empty_response_uses_fallback(self, mock_graph, mock_client):
        """If LLM returns empty content, consumer should write fallback to po:response."""
        mock_graph.ainvoke.return_value = {"messages": [AIMessage(content="")]}
        data = {
            "type": "user_message",
            "text": "don't respond",
            "request_id": "req-empty",
        }

        await _handle_message(mock_graph, mock_client, "user-1", data)

        mock_client.publish_flat.assert_called_once()
        call_args = mock_client.publish_flat.call_args
        assert call_args[0][0] == "po:response:req-empty"
        assert call_args[0][1]["text"] == "Бот вернул пустой ответ"

    @pytest.mark.asyncio
    async def test_handles_missing_timestamp(self, mock_graph, mock_client):
        data = {"type": "user_message", "text": "no timestamp", "request_id": "req-1"}

        await _handle_message(mock_graph, mock_client, "user-1", data)

        msg = mock_graph.ainvoke.call_args[0][0]["messages"][0]
        assert msg.content == "no timestamp"

    @pytest.mark.asyncio
    async def test_user_message_includes_user_id_in_config(self, mock_graph, mock_client):
        """user_id should be passed in configurable for tools to read."""
        data = {"type": "user_message", "text": "hi", "request_id": "req-1"}

        await _handle_message(mock_graph, mock_client, "user-42", data)

        config = mock_graph.ainvoke.call_args[1]["config"]
        assert config["configurable"]["user_id"] == "user-42"

    @pytest.mark.asyncio
    async def test_system_event_includes_user_id_in_config(self, mock_graph, mock_client):
        """System events should also pass user_id in config."""
        data = {
            "type": "system_event",
            "event": "completed",
            "text": "engineering_completed",
            "user_id": "user-99",
        }

        await _handle_message(mock_graph, mock_client, "user-99", data)

        config = mock_graph.ainvoke.call_args[1]["config"]
        assert config["configurable"]["user_id"] == "user-99"

    @pytest.mark.asyncio
    async def test_empty_response_without_request_id_stays_silent(self, mock_graph, mock_client):
        """Empty response without request_id should NOT write to po:proactive."""
        mock_graph.ainvoke.return_value = {"messages": [AIMessage(content="")]}
        data = {
            "type": "system_event",
            "event": "completed",
            "text": "scaffolding_completed",
        }

        await _handle_message(mock_graph, mock_client, "user-1", data)

        mock_graph.ainvoke.assert_called_once()
        mock_client.publish_flat.assert_not_called()

    @pytest.mark.asyncio
    async def test_progress_event_dropped(self, mock_graph, mock_client):
        """Progress system events should not invoke the LLM."""
        data = {
            "type": "system_event",
            "event": "progress",
            "text": "Waiting for CI checks...",
            "timestamp": "2026-02-15T10:00:00",
        }

        await _handle_message(mock_graph, mock_client, "user-1", data)

        mock_graph.ainvoke.assert_not_called()
        mock_client.publish_flat.assert_not_called()

    @pytest.mark.asyncio
    async def test_completed_event_includes_event_type(self, mock_graph, mock_client):
        """Completed events should have event type in format tag."""
        data = {
            "type": "system_event",
            "event": "completed",
            "text": "Deploy completed",
            "timestamp": "2026-02-15T10:00:00",
        }

        await _handle_message(mock_graph, mock_client, "user-1", data)

        msg = mock_graph.ainvoke.call_args[0][0]["messages"][0]
        assert "[system: system_event:completed]" in msg.content

    @pytest.mark.asyncio
    async def test_failed_event_includes_event_type(self, mock_graph, mock_client):
        """Failed events should have event type in format tag."""
        data = {
            "type": "system_event",
            "event": "failed",
            "text": "Engineering failed: timeout",
            "timestamp": "2026-02-15T10:00:00",
        }

        await _handle_message(mock_graph, mock_client, "user-1", data)

        msg = mock_graph.ainvoke.call_args[0][0]["messages"][0]
        assert "[system: system_event:failed]" in msg.content

    @pytest.mark.asyncio
    async def test_system_event_without_event_field_dropped(self, mock_graph, mock_client):
        """System events without event field should be dropped."""
        data = {
            "type": "system_event",
            "text": "legacy event",
            "timestamp": "2026-02-15T10:00:00",
        }

        await _handle_message(mock_graph, mock_client, "user-1", data)

        mock_graph.ainvoke.assert_not_called()
        mock_client.publish_flat.assert_not_called()


class TestProcessMessage:
    @pytest.mark.asyncio
    async def test_acks_message_on_success(self, mock_graph, mock_client):
        sem = asyncio.Semaphore(10)
        user_locks: dict[str, asyncio.Lock] = {}
        data = {"type": "user_message", "text": "hi", "user_id": "u1", "request_id": "r1"}

        await _process_message(mock_graph, mock_client, sem, user_locks, "msg-1", data)

        mock_client.redis.xack.assert_called_once_with("po:input", "po-consumer", "msg-1")

    @pytest.mark.asyncio
    async def test_acks_message_on_error(self, mock_graph, mock_client):
        mock_graph.ainvoke.side_effect = RuntimeError("LLM API down")
        sem = asyncio.Semaphore(10)
        user_locks: dict[str, asyncio.Lock] = {}
        data = {"type": "user_message", "text": "hi", "user_id": "u1", "request_id": "r1"}

        await _process_message(mock_graph, mock_client, sem, user_locks, "msg-1", data)

        # xack in finally — always called
        mock_client.redis.xack.assert_called_once()

    @pytest.mark.asyncio
    async def test_sends_error_response_on_failure(self, mock_graph, mock_client):
        mock_graph.ainvoke.side_effect = RuntimeError("boom")
        sem = asyncio.Semaphore(10)
        user_locks: dict[str, asyncio.Lock] = {}
        data = {"type": "user_message", "text": "hi", "user_id": "u1", "request_id": "r1"}

        await _process_message(mock_graph, mock_client, sem, user_locks, "msg-1", data)

        # Error response written
        xadd_calls = mock_client.publish_flat.call_args_list
        assert len(xadd_calls) == 1
        assert xadd_calls[0][0][0] == "po:response:r1"
        assert xadd_calls[0][0][1]["error"] == "true"

    @pytest.mark.asyncio
    async def test_per_user_serialization(self, mock_graph, mock_client):
        """Messages from same user should be serialized via lock."""
        call_order = []
        gate = asyncio.Event()

        async def slow_invoke(input_data, config):
            user = config["configurable"]["thread_id"]
            call_order.append(f"start-{user}")
            gate.set()
            await asyncio.sleep(0)  # yield control
            call_order.append(f"end-{user}")
            return {"messages": [AIMessage(content="ok")]}

        mock_graph.ainvoke.side_effect = slow_invoke

        sem = asyncio.Semaphore(10)
        user_locks: dict[str, asyncio.Lock] = {}

        data1 = {"type": "user_message", "text": "m1", "user_id": "u1", "request_id": "r1"}
        data2 = {"type": "user_message", "text": "m2", "user_id": "u1", "request_id": "r2"}

        # Run both concurrently — should be serialized for same user
        await asyncio.gather(
            _process_message(mock_graph, mock_client, sem, user_locks, "id1", data1),
            _process_message(mock_graph, mock_client, sem, user_locks, "id2", data2),
        )

        # Verify serialization: first must end before second starts
        start_indices = [i for i, x in enumerate(call_order) if x.startswith("start")]
        end_indices = [i for i, x in enumerate(call_order) if x.startswith("end")]
        assert end_indices[0] < start_indices[1]

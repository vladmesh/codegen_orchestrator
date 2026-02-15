"""Unit tests for PO consumer."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
import pytest

from src.po.consumer import _handle_message, _process_message


@pytest.fixture
def mock_graph():
    """Mock compiled graph with ainvoke."""
    graph = AsyncMock()
    graph.ainvoke.return_value = {"messages": [AIMessage(content="Hello! How can I help?")]}
    return graph


@pytest.fixture
def mock_redis():
    """Mock Redis client."""
    redis = AsyncMock()
    return redis


class TestHandleMessage:
    @pytest.mark.asyncio
    async def test_user_message_creates_human_message(self, mock_graph, mock_redis):
        data = {
            "type": "user_message",
            "text": "hello",
            "timestamp": "2026-02-15T10:00:00",
            "request_id": "req-1",
        }

        await _handle_message(mock_graph, mock_redis, "user-1", data)

        mock_graph.ainvoke.assert_called_once()
        call_args = mock_graph.ainvoke.call_args
        msg = call_args[0][0]["messages"][0]
        assert isinstance(msg, HumanMessage)
        assert "2026-02-15T10:00:00" in msg.content
        assert "hello" in msg.content

    @pytest.mark.asyncio
    async def test_system_event_creates_system_message(self, mock_graph, mock_redis):
        data = {
            "type": "system_event",
            "text": "engineering_completed",
            "timestamp": "2026-02-15T10:00:00",
            "request_id": "req-1",
        }

        await _handle_message(mock_graph, mock_redis, "user-1", data)

        msg = mock_graph.ainvoke.call_args[0][0]["messages"][0]
        assert isinstance(msg, SystemMessage)

    @pytest.mark.asyncio
    async def test_reminder_creates_system_message(self, mock_graph, mock_redis):
        data = {
            "type": "reminder",
            "text": "check task eng-123",
            "timestamp": "2026-02-15T10:00:00",
        }

        await _handle_message(mock_graph, mock_redis, "user-1", data)

        msg = mock_graph.ainvoke.call_args[0][0]["messages"][0]
        assert isinstance(msg, SystemMessage)

    @pytest.mark.asyncio
    async def test_uses_thread_id_per_user(self, mock_graph, mock_redis):
        data = {"type": "user_message", "text": "hi", "request_id": "req-1"}

        await _handle_message(mock_graph, mock_redis, "user-42", data)

        config = mock_graph.ainvoke.call_args[1]["config"]
        assert config["configurable"]["thread_id"] == "po-user-user-42"

    @pytest.mark.asyncio
    async def test_writes_response_with_request_id(self, mock_graph, mock_redis):
        data = {
            "type": "user_message",
            "text": "hello",
            "request_id": "req-123",
        }

        await _handle_message(mock_graph, mock_redis, "user-1", data)

        mock_redis.xadd.assert_called_once()
        call_args = mock_redis.xadd.call_args
        assert call_args[0][0] == "po:response:req-123"
        assert call_args[0][1]["text"] == "Hello! How can I help?"
        assert call_args[0][1]["user_id"] == "user-1"

    @pytest.mark.asyncio
    async def test_no_response_without_request_id(self, mock_graph, mock_redis):
        data = {"type": "system_event", "text": "scaffolding_done"}

        await _handle_message(mock_graph, mock_redis, "user-1", data)

        mock_redis.xadd.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_missing_timestamp(self, mock_graph, mock_redis):
        data = {"type": "user_message", "text": "no timestamp", "request_id": "req-1"}

        await _handle_message(mock_graph, mock_redis, "user-1", data)

        msg = mock_graph.ainvoke.call_args[0][0]["messages"][0]
        assert msg.content == "no timestamp"

    @pytest.mark.asyncio
    async def test_user_message_includes_user_id_in_config(self, mock_graph, mock_redis):
        """user_id should be passed in configurable for tools to read."""
        data = {"type": "user_message", "text": "hi", "request_id": "req-1"}

        await _handle_message(mock_graph, mock_redis, "user-42", data)

        config = mock_graph.ainvoke.call_args[1]["config"]
        assert config["configurable"]["user_id"] == "user-42"

    @pytest.mark.asyncio
    async def test_system_event_includes_user_id_in_config(self, mock_graph, mock_redis):
        """System events should also pass user_id in config."""
        data = {
            "type": "system_event",
            "text": "engineering_completed",
            "user_id": "user-99",
        }

        await _handle_message(mock_graph, mock_redis, "user-99", data)

        config = mock_graph.ainvoke.call_args[1]["config"]
        assert config["configurable"]["user_id"] == "user-99"

    @pytest.mark.asyncio
    async def test_system_event_without_request_id_skips_response(self, mock_graph, mock_redis):
        """System events without request_id should not write to po:response:*."""
        data = {
            "type": "system_event",
            "text": "engineering_completed",
        }

        await _handle_message(mock_graph, mock_redis, "user-1", data)

        # Graph should be invoked
        mock_graph.ainvoke.assert_called_once()
        # But no response written
        mock_redis.xadd.assert_not_called()


class TestProcessMessage:
    @pytest.mark.asyncio
    async def test_acks_message_on_success(self, mock_graph, mock_redis):
        sem = asyncio.Semaphore(10)
        user_locks: dict[str, asyncio.Lock] = {}
        data = {"type": "user_message", "text": "hi", "user_id": "u1", "request_id": "r1"}

        await _process_message(mock_graph, mock_redis, sem, user_locks, "msg-1", data)

        mock_redis.xack.assert_called_once_with("po:input", "po-consumer", "msg-1")

    @pytest.mark.asyncio
    async def test_acks_message_on_error(self, mock_graph, mock_redis):
        mock_graph.ainvoke.side_effect = RuntimeError("LLM API down")
        sem = asyncio.Semaphore(10)
        user_locks: dict[str, asyncio.Lock] = {}
        data = {"type": "user_message", "text": "hi", "user_id": "u1", "request_id": "r1"}

        await _process_message(mock_graph, mock_redis, sem, user_locks, "msg-1", data)

        # xack in finally — always called
        mock_redis.xack.assert_called_once()

    @pytest.mark.asyncio
    async def test_sends_error_response_on_failure(self, mock_graph, mock_redis):
        mock_graph.ainvoke.side_effect = RuntimeError("boom")
        sem = asyncio.Semaphore(10)
        user_locks: dict[str, asyncio.Lock] = {}
        data = {"type": "user_message", "text": "hi", "user_id": "u1", "request_id": "r1"}

        await _process_message(mock_graph, mock_redis, sem, user_locks, "msg-1", data)

        # Error response written
        xadd_calls = mock_redis.xadd.call_args_list
        assert len(xadd_calls) == 1
        assert xadd_calls[0][0][0] == "po:response:r1"
        assert xadd_calls[0][0][1]["error"] == "true"

    @pytest.mark.asyncio
    async def test_per_user_serialization(self, mock_graph, mock_redis):
        """Messages from same user should be serialized via lock."""
        call_order = []

        async def slow_invoke(input_data, config):
            user = config["configurable"]["thread_id"]
            call_order.append(f"start-{user}")
            await asyncio.sleep(0.05)
            call_order.append(f"end-{user}")
            return {"messages": [AIMessage(content="ok")]}

        mock_graph.ainvoke.side_effect = slow_invoke

        sem = asyncio.Semaphore(10)
        user_locks: dict[str, asyncio.Lock] = {}

        data1 = {"type": "user_message", "text": "m1", "user_id": "u1", "request_id": "r1"}
        data2 = {"type": "user_message", "text": "m2", "user_id": "u1", "request_id": "r2"}

        # Run both concurrently — should be serialized for same user
        await asyncio.gather(
            _process_message(mock_graph, mock_redis, sem, user_locks, "id1", data1),
            _process_message(mock_graph, mock_redis, sem, user_locks, "id2", data2),
        )

        # Verify serialization: first must end before second starts
        start_indices = [i for i, x in enumerate(call_order) if x.startswith("start")]
        end_indices = [i for i, x in enumerate(call_order) if x.startswith("end")]
        assert end_indices[0] < start_indices[1]

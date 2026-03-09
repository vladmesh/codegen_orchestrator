"""Unit tests for PO consumer."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
import pytest

from src.consumers.po import _handle_message, _process_message, _repair_orphan_tool_calls


@pytest.fixture
def mock_graph():
    """Mock compiled graph with ainvoke and aget_state."""
    graph = AsyncMock()
    graph.ainvoke.return_value = {"messages": [AIMessage(content="Hello! How can I help?")]}
    # Default: clean checkpoint (no orphans)
    clean_state = AsyncMock()
    clean_state.values = {"messages": []}
    graph.aget_state.return_value = clean_state
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
    async def test_system_event_dropped(self, mock_graph, mock_client):
        """All system_event messages are dropped — PO only checks status via reminders."""
        data = {
            "type": "system_event",
            "event": "completed",
            "text": "engineering_completed",
            "timestamp": "2026-02-15T10:00:00",
            "request_id": "req-1",
        }

        await _handle_message(mock_graph, mock_client, "user-1", data)

        mock_graph.ainvoke.assert_not_called()

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
        data = {"type": "reminder", "text": "check story story-abc12345"}

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
        assert "no timestamp" in msg.content
        # No timestamp prefix (no "[...UTC]")
        assert "UTC]" not in msg.content

    @pytest.mark.asyncio
    async def test_user_message_includes_user_id_in_config(self, mock_graph, mock_client):
        """user_id should be passed in configurable for tools to read."""
        data = {"type": "user_message", "text": "hi", "request_id": "req-1"}

        await _handle_message(mock_graph, mock_client, "user-42", data)

        config = mock_graph.ainvoke.call_args[1]["config"]
        assert config["configurable"]["user_id"] == "user-42"

    @pytest.mark.asyncio
    async def test_reminder_includes_user_id_in_config(self, mock_graph, mock_client):
        """Reminders should pass user_id in config."""
        data = {
            "type": "reminder",
            "text": "check story story-abc12345",
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
            "type": "reminder",
            "text": "check story story-abc12345",
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
    @pytest.mark.parametrize("event_type", ["completed", "failed", "progress", ""])
    async def test_all_system_events_dropped(self, mock_graph, mock_client, event_type):
        """All system_event messages are dropped — PO uses reminders for status."""
        data = {
            "type": "system_event",
            "event": event_type,
            "text": "some event",
            "timestamp": "2026-02-15T10:00:00",
        }

        await _handle_message(mock_graph, mock_client, "user-1", data)

        mock_graph.ainvoke.assert_not_called()
        mock_client.publish_flat.assert_not_called()

    @pytest.mark.asyncio
    async def test_user_name_passed_in_configurable(self, mock_graph, mock_client):
        """user_name from message data should appear in graph configurable."""
        data = {
            "type": "user_message",
            "text": "hello",
            "user_id": "42",
            "user_name": "Vlad",
            "request_id": "req-1",
            "timestamp": "2026-01-01T00:00:00",
        }

        await _handle_message(mock_graph, mock_client, "42", data)

        config = mock_graph.ainvoke.call_args[1]["config"]
        assert config["configurable"]["user_name"] == "Vlad"

    @pytest.mark.asyncio
    async def test_user_name_empty_when_missing(self, mock_graph, mock_client):
        """user_name defaults to empty string when not in data."""
        data = {
            "type": "user_message",
            "text": "hello",
            "user_id": "42",
            "request_id": "req-1",
        }

        await _handle_message(mock_graph, mock_client, "42", data)

        config = mock_graph.ainvoke.call_args[1]["config"]
        assert config["configurable"]["user_name"] == ""

    @pytest.mark.asyncio
    async def test_user_message_includes_context_prefix(self, mock_graph, mock_client):
        """User messages should include context prefix with user_id and user_name."""
        data = {
            "type": "user_message",
            "text": "hello",
            "user_id": "42",
            "user_name": "Vlad",
            "request_id": "req-1",
            "timestamp": "2026-01-01T00:00:00",
        }

        await _handle_message(mock_graph, mock_client, "42", data)

        messages = mock_graph.ainvoke.call_args[0][0]["messages"]
        content = messages[0].content
        assert "user_id=42" in content
        assert "user_name=Vlad" in content


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


class TestRepairOrphanToolCalls:
    @pytest.mark.asyncio
    async def test_no_state_returns_zero(self):
        """No checkpoint (new thread) — nothing to repair."""
        graph = AsyncMock()
        graph.aget_state.return_value = AsyncMock(values={})

        result = await _repair_orphan_tool_calls(graph, "po-user-1")
        assert result == 0
        graph.aupdate_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_clean_history_returns_zero(self):
        """All tool_calls have matching ToolMessages — nothing to repair."""
        ai_msg = AIMessage(
            content="",
            tool_calls=[{"id": "tc-1", "name": "get_projects", "args": {}}],
        )
        tool_msg = ToolMessage(content="[]", tool_call_id="tc-1")
        state = AsyncMock()
        state.values = {"messages": [ai_msg, tool_msg]}

        graph = AsyncMock()
        graph.aget_state.return_value = state

        result = await _repair_orphan_tool_calls(graph, "po-user-1")
        assert result == 0
        graph.aupdate_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_orphan_tool_call_gets_repaired(self):
        """AIMessage with tool_call but no ToolMessage — should inject recovery ToolMessage."""
        ai_msg = AIMessage(
            content="Let me check",
            tool_calls=[{"id": "tc-orphan", "name": "get_task_status", "args": {}}],
        )
        state = AsyncMock()
        state.values = {"messages": [ai_msg]}

        graph = AsyncMock()
        graph.aget_state.return_value = state

        result = await _repair_orphan_tool_calls(graph, "po-user-1")

        assert result == 1
        graph.aupdate_state.assert_called_once()
        call_args = graph.aupdate_state.call_args
        config = call_args[0][0]
        assert config["configurable"]["thread_id"] == "po-user-1"
        injected = call_args[0][1]["messages"]
        assert len(injected) == 1
        assert isinstance(injected[0], ToolMessage)
        assert injected[0].tool_call_id == "tc-orphan"

    @pytest.mark.asyncio
    async def test_multiple_orphans_all_repaired(self):
        """Multiple orphan tool_calls across multiple AIMessages."""
        ai1 = AIMessage(
            content="",
            tool_calls=[{"id": "tc-1", "name": "tool_a", "args": {}}],
        )
        tool1 = ToolMessage(content="ok", tool_call_id="tc-1")
        ai2 = AIMessage(
            content="",
            tool_calls=[
                {"id": "tc-2", "name": "tool_b", "args": {}},
                {"id": "tc-3", "name": "tool_c", "args": {}},
            ],
        )
        # tc-2 and tc-3 have no ToolMessages
        state = AsyncMock()
        state.values = {"messages": [ai1, tool1, ai2]}

        graph = AsyncMock()
        graph.aget_state.return_value = state

        result = await _repair_orphan_tool_calls(graph, "po-user-1")

        assert result == 2
        injected = graph.aupdate_state.call_args[0][1]["messages"]
        assert len(injected) == 2
        ids = {m.tool_call_id for m in injected}
        assert ids == {"tc-2", "tc-3"}


class TestHandleMessageRecovery:
    """Tests for checkpoint recovery integration in _handle_message."""

    @pytest.mark.asyncio
    async def test_pre_invoke_repair_called(self, mock_graph, mock_client):
        """_repair_orphan_tool_calls is called before graph.ainvoke."""
        with patch(
            "src.consumers.po._repair_orphan_tool_calls", new_callable=AsyncMock
        ) as mock_repair:
            mock_repair.return_value = 0
            data = {"type": "user_message", "text": "hi", "request_id": "req-1"}
            await _handle_message(mock_graph, mock_client, "user-1", data)

            mock_repair.assert_called_once_with(mock_graph, "po-user-user-1")
            mock_graph.ainvoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_retry_on_corrupt_checkpoint_valueerror(self, mock_graph, mock_client):
        """If ainvoke raises ValueError about orphan tool_calls, repair and retry."""
        corrupt_error = ValueError(
            "Found AIMessages with tool_calls that do not have a corresponding ToolMessage"
        )
        mock_graph.ainvoke.side_effect = [corrupt_error, {"messages": [AIMessage(content="ok")]}]

        with patch(
            "src.consumers.po._repair_orphan_tool_calls", new_callable=AsyncMock
        ) as mock_repair:
            mock_repair.return_value = 0  # pre-check finds nothing (race condition)
            data = {"type": "user_message", "text": "hi", "request_id": "req-1"}
            await _handle_message(mock_graph, mock_client, "user-1", data)

            # repair called twice: once pre-invoke, once on error
            assert mock_repair.call_count == 2
            assert mock_graph.ainvoke.call_count == 2
            # Response should be written successfully
            mock_client.publish_flat.assert_called_once()

    @pytest.mark.asyncio
    async def test_unrelated_valueerror_not_caught(self, mock_graph, mock_client):
        """ValueError not about tool_calls should propagate."""
        mock_graph.ainvoke.side_effect = ValueError("some other error")

        with patch(
            "src.consumers.po._repair_orphan_tool_calls", new_callable=AsyncMock
        ) as mock_repair:
            mock_repair.return_value = 0
            data = {"type": "user_message", "text": "hi", "request_id": "req-1"}
            with pytest.raises(ValueError, match="some other error"):
                await _handle_message(mock_graph, mock_client, "user-1", data)

    @pytest.mark.asyncio
    async def test_retry_fails_again_propagates(self, mock_graph, mock_client):
        """If retry also fails with same error, it should propagate."""
        corrupt_error = ValueError(
            "Found AIMessages with tool_calls that do not have a corresponding ToolMessage"
        )
        mock_graph.ainvoke.side_effect = [corrupt_error, corrupt_error]

        with patch(
            "src.consumers.po._repair_orphan_tool_calls", new_callable=AsyncMock
        ) as mock_repair:
            mock_repair.return_value = 0
            data = {"type": "user_message", "text": "hi", "request_id": "req-1"}
            with pytest.raises(ValueError, match="tool_calls"):
                await _handle_message(mock_graph, mock_client, "user-1", data)


class TestRepairWithRealGraph:
    """Integration test using MemorySaver — real graph state, no mocks for checkpoint."""

    @pytest.fixture
    def real_graph(self):
        """Create a minimal react agent with MemorySaver for testing checkpoint repair."""
        from langchain_core.messages import AIMessage as AI
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.graph import MessagesState, StateGraph

        # Minimal graph that just echoes
        def echo(state: MessagesState):
            return {"messages": [AI(content="recovered ok")]}

        builder = StateGraph(MessagesState)
        builder.add_node("echo", echo)
        builder.set_entry_point("echo")
        builder.set_finish_point("echo")
        return builder.compile(checkpointer=MemorySaver())

    @pytest.mark.asyncio
    async def test_repair_corrupted_checkpoint_with_real_graph(self, real_graph):
        """Manually corrupt a checkpoint and verify _repair_orphan_tool_calls fixes it."""
        thread_id = "test-corrupt-thread"
        config = {"configurable": {"thread_id": thread_id}}

        # Step 1: Run graph to create initial checkpoint
        await real_graph.ainvoke({"messages": [HumanMessage(content="hello")]}, config=config)

        # Step 2: Corrupt the checkpoint by injecting an AIMessage with orphan tool_calls
        orphan_ai = AIMessage(
            content="Let me check",
            tool_calls=[{"id": "tc-orphan-1", "name": "get_task_status", "args": {}}],
        )
        await real_graph.aupdate_state(config, {"messages": [orphan_ai]})

        # Verify corruption: state now has orphan tool_call
        state = await real_graph.aget_state(config)
        ai_tool_call_ids = {
            tc["id"]
            for m in state.values["messages"]
            if isinstance(m, AIMessage)
            for tc in m.tool_calls
        }
        tool_result_ids = {
            m.tool_call_id for m in state.values["messages"] if isinstance(m, ToolMessage)
        }
        orphans_before = ai_tool_call_ids - tool_result_ids
        assert "tc-orphan-1" in orphans_before

        # Step 3: Repair
        repaired = await _repair_orphan_tool_calls(real_graph, thread_id)
        assert repaired == 1

        # Step 4: Verify state is now clean
        state_after = await real_graph.aget_state(config)
        tool_result_ids_after = {
            m.tool_call_id for m in state_after.values["messages"] if isinstance(m, ToolMessage)
        }
        assert "tc-orphan-1" in tool_result_ids_after

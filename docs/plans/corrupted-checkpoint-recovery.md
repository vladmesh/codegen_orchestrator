# Plan: Corrupted Checkpoint Recovery (#48)

## Context

PO agent crashes with `ValueError: Found AIMessages with tool_calls that do not have a corresponding ToolMessage` when checkpoint contains an AIMessage with `tool_calls` but no paired ToolMessage. This happens when:

1. PO processes a message (e.g. reminder), LLM returns tool_calls
2. Tool execution crashes/times out, or consumer crashes mid-invocation
3. Checkpoint saves the AIMessage with tool_calls but no ToolMessage is recorded
4. Next invocation loads corrupted state → `_validate_chat_history` in `_get_model_input_state` raises ValueError
5. User is permanently blocked — every subsequent message hits the same error

The validation (`_validate_chat_history`) runs inside LangGraph's `acall_model` at `create_react_agent` line 371. It checks all messages in the thread, not just the new ones.

**Key file**: `services/langgraph/src/po/consumer.py` — `_handle_message` (line 203)

**Recovery strategy**: Before calling `graph.ainvoke()`, use `graph.aget_state()` to inspect the checkpoint. If orphan tool_calls exist, use `graph.aupdate_state()` to inject error ToolMessages, healing the checkpoint before invocation.

## Steps

1. [ ] Add `_repair_orphan_tool_calls` helper
   - **Input**: `services/langgraph/src/po/consumer.py`, `graph` (CompiledStateGraph), `thread_id` (str)
   - **Output**: New async function `_repair_orphan_tool_calls(graph, thread_id) -> int` that:
     1. Calls `graph.aget_state(config)` to load current checkpoint
     2. Scans messages for AIMessages with tool_calls that have no paired ToolMessage
     3. If orphans found: calls `graph.aupdate_state(config, {"messages": [ToolMessage(...) for each orphan]})` with error content like `"[recovery] Tool call interrupted — result unavailable."`
     4. Returns count of repaired tool_calls (0 if clean)
   - **Test**: Unit test with mocked graph — verify ToolMessages are injected for orphans, no-op when clean

2. [ ] Integrate repair into `_handle_message`
   - **Input**: `services/langgraph/src/po/consumer.py` — `_handle_message` function (line 203)
   - **Output**: Call `_repair_orphan_tool_calls(graph, thread_id)` before `graph.ainvoke()`. Log warning with repair count if > 0. Thread ID is already computed as `f"po-user-{user_id}"`.
   - **Test**: Unit test — mock graph.ainvoke to raise ValueError on first call (simulating corrupted state), verify repair is called, then ainvoke succeeds on retry. Test normal flow (no orphans) still works.

3. [ ] Add retry-on-corruption fallback in `_handle_message`
   - **Input**: `services/langgraph/src/po/consumer.py` — `_handle_message`
   - **Output**: Wrap `graph.ainvoke()` in try/except for ValueError matching "tool_calls that do not have a corresponding ToolMessage". On catch: call `_repair_orphan_tool_calls`, retry ainvoke once. This handles edge cases where the pre-check passes but new corruption happens between check and invoke (race with summarization hook or concurrent checkpoint writes).
   - **Test**: Unit test — graph.ainvoke raises ValueError first time, repair fixes it, second ainvoke succeeds. Also test: non-matching ValueError is NOT caught (re-raised).

4. [ ] Integration test with real LangGraph state
   - **Input**: `services/langgraph/tests/unit/po/test_consumer.py` (can use MemorySaver for in-memory checkpoint)
   - **Output**: Test that creates a real graph with MemorySaver, manually corrupts checkpoint (insert AIMessage with tool_calls, no ToolMessage), then invokes `_handle_message` and verifies recovery + successful response. This tests the full flow without mocks.
   - **Test**: Self-contained integration test in the unit test file (MemorySaver needs no external deps)

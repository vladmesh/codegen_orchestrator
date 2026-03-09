# #36 Architect: migrate from scheduler function to LangGraph ReAct agent

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

The Architect is currently a plain function (`decompose_story()`) in `services/scheduler/src/tasks/architect_consumer.py`. It does a single LLM call via raw httpx, parses JSON, and creates tasks via API. This is inconsistent with other agents (PO, engineering, deploy) that use LangGraph ReAct agents.

This migration moves the architect into `services/langgraph/` as a ReAct agent with proper tool use, reasoning loop, and the standard consumer pattern (`_base.py`). The scheduler will no longer contain architect code.

**Current state**: architect_consumer.py in scheduler (266 lines), unit tests in scheduler (3 test files), no LangGraph architect code exists.

## Steps

1. [ ] Add story/task methods to LanggraphAPIClient
   - **Input**: `services/langgraph/src/clients/api.py`
   - **Output**: New methods: `get_story()`, `get_project()` (already exists), `get_tasks_by_story()`, `create_task()`, `transition_story()`
   - **Test**: Unit test mocking httpx calls to verify correct API paths and return values

2. [ ] Create architect state and tools
   - **Input**: `services/langgraph/src/architect/` (new directory)
   - **Output**: `state.py` (ArchitectState TypedDict), `tools.py` (5 tools: create_task, get_story, get_project_spec, get_tasks_by_story, transition_story — all using LanggraphAPIClient)
   - **Test**: Unit tests for each tool — mock api_client, verify correct calls and return formatting

3. [ ] Create architect system prompt
   - **Input**: Current `DECOMPOSE_SYSTEM_PROMPT` in `architect_consumer.py`, `services/langgraph/src/prompts/po/__init__.py` for pattern reference
   - **Output**: `services/langgraph/src/prompts/architect/__init__.py` with `SYSTEM_PROMPT` constant
   - **Test**: Assert prompt is non-empty string, contains key instructions (task types, dependency rules)

4. [ ] Create architect ReAct agent graph
   - **Input**: `services/langgraph/src/po/graph.py` for pattern, architect tools + state + prompt from steps 2-3
   - **Output**: `services/langgraph/src/architect/graph.py` with `create_architect_graph()` — binds tools to LLM via `create_react_agent`, no summarization needed (short-lived sessions), MemorySaver only (no persistent checkpointer needed for one-shot decomposition)
   - **Test**: Unit test that graph compiles without error, has expected node names

5. [ ] Create architect consumer (Redis stream → graph)
   - **Input**: `services/langgraph/src/consumers/_base.py` for `start_worker` pattern, `services/langgraph/src/consumers/deploy.py` for reference
   - **Output**: `services/langgraph/src/consumers/architect.py` with `process_architect_job()` and `main()` entrypoint. Validates `ArchitectMessage`, builds initial state with story/project context, invokes graph, handles errors.
   - **Test**: Unit test — mock graph.ainvoke, verify message validation, state construction, error handling

6. [ ] Add architect settings to LangGraph config
   - **Input**: `services/langgraph/src/config/settings.py`
   - **Output**: Add `architect_llm_model`, `architect_llm_base_url`, `architect_llm_api_key` fields (all optional, same pattern as PO)
   - **Test**: Unit test — verify settings load from env vars, None when unset

7. [ ] Docker: add architect-worker service
   - **Input**: `docker-compose.yml`, `docker/test/integration/backend.yml`
   - **Output**: New `architect-worker` service (same image as langgraph, command: `python -m src.consumers.architect`), env vars for ARCHITECT_LLM_*, depends_on api+redis. Integration test backend entry.
   - **Test**: `docker compose config --services` includes architect-worker

8. [ ] Remove architect from scheduler
   - **Input**: `services/scheduler/src/tasks/architect_consumer.py`, `services/scheduler/src/main.py`
   - **Output**: Delete `architect_consumer.py`, remove import + `asyncio.create_task(architect_consumer_loop())` from `main.py`, remove ARCHITECT_LLM_* env vars from scheduler docker-compose section (if any)
   - **Test**: `make test-scheduler-unit` passes, grep confirms no architect references in scheduler (except queue constants in shared/)

9. [ ] Migrate and update unit tests
   - **Input**: `services/scheduler/tests/unit/test_architect_consumer.py`, `test_architect_contract.py`, `test_architect_pipeline_flow.py`
   - **Output**: New tests in `services/langgraph/tests/unit/test_architect_*.py` adapted for the new graph/tools/consumer structure. Delete old scheduler tests.
   - **Test**: `make test-langgraph-unit` passes with new architect tests

10. [ ] Integration test: end-to-end architect flow
    - **Input**: All components from steps 1-9
    - **Output**: Integration test in `services/langgraph/tests/integration/test_architect_integration.py` — publish ArchitectMessage to Redis, verify tasks created in DB via API
    - **Test**: `make test-langgraph-integration` passes


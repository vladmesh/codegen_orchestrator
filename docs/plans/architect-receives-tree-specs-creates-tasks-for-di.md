# Architect receives tree + specs, creates tasks for diff

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Architect agent currently has a generic prompt: "decompose story into tasks, foundational work first." It doesn't know about the scaffolded project state — no tree, no awareness of what already exists. This causes over-decomposition (5 tasks for a simple bot instead of 1-2).

The scaffolder service (already implemented) saves project tree to `project.config["tree"]` after scaffolding. The architect needs to see this tree and create tasks only for the diff between scaffolded state and story requirements.

Additionally, the architect consumer needs to auto-append a CI check task after the LLM finishes creating tasks.

**Key files**:
- `services/langgraph/src/agents/architect/tools.py` — tool definitions
- `services/langgraph/src/prompts/architect/__init__.py` — system prompt
- `services/langgraph/src/consumers/architect.py` — consumer (post-processing)
- `services/langgraph/tests/unit/test_architect_tools.py` — tool tests
- `services/langgraph/tests/unit/test_architect_consumer.py` — consumer tests

## Steps

1. [ ] Enhance `get_project_spec` tool to surface tree and key spec fields
   - **Input**: `services/langgraph/src/agents/architect/tools.py`
   - **Output**: `get_project_spec` extracts `config.tree` into a top-level `tree` key in the response. Also extracts `project_spec` if present. Removes noisy fields (secrets, env_hints) from config before returning to LLM — save tokens.
   - **Test**: Unit test: mock `api_client.get_project` returning `{"config": {"tree": "...", "secrets": {...}}, "project_spec": {...}}` → verify `tree` is top-level, `secrets` stripped. Test with missing tree (not yet scaffolded) → no crash.

2. [ ] Rewrite architect system prompt for scaffolded-aware decomposition
   - **Input**: `services/langgraph/src/prompts/architect/__init__.py`
   - **Output**: New prompt instructs architect to: (a) see project as already scaffolded, (b) create tasks only for business logic diff, (c) never create infra/Docker/CI tasks, (d) not specify implementation details (worker has AGENTS.md), (e) aim for 1-2 tasks on simple projects, max 3 on medium. Workflow steps remain the same.
   - **Test**: Unit test: verify `SYSTEM_PROMPT` contains key phrases ("scaffolded", "AGENTS.md", "do NOT create tasks for infrastructure"). Snapshot-style — ensures prompt drift is caught.

3. [ ] Auto-append CI check task after architect LLM finishes
   - **Input**: `services/langgraph/src/consumers/architect.py`, `services/langgraph/src/clients/api.py`
   - **Output**: After `graph.ainvoke()` succeeds, consumer fetches tasks created for this story via API, finds the last one (by dependency chain or creation order), creates a CI check task: title="Run tests, verify CI green", description="Run full test suite. Push to GitHub. Wait for CI. If CI fails, fix and retry.", type="feature", blocked_by=last architect task ID, created_by="system". Uses `api_client.create_task()`.
   - **Test**: Unit test: mock graph.ainvoke + api_client → verify CI task created with correct blocked_by, created_by="system". Test edge case: architect created 0 tasks (duplicates) → no CI task appended.

4. [ ] Integration test: full architect flow produces correct task chain
   - **Input**: `services/langgraph/tests/unit/test_architect_consumer.py` (or new integration test file)
   - **Output**: Test that `process_architect_job` with mocked graph output + mocked API produces: (a) architect tasks in order, (b) CI task appended at end, (c) CI task blocked_by last architect task, (d) story transitioned to in_progress. Mock the LLM but use real consumer logic.
   - **Test**: This IS the integration test.


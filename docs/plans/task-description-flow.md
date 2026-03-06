# Plan: Fix Description Loss in Create Flow (#50)

## Context

При `action=create` description, собранное PO в диалоге с пользователем, теряется по пути к воркеру. PO передаёт description в `trigger_engineering(description=...)`, оно попадает в Redis queue, engineering worker кладёт его в `state["description"]`, но developer node (`_build_create_task`) его игнорирует — использует только `config.description` (короткое) и `project_spec.detailed_spec` (N/A).

Для `action=feature/fix` всё работает — `_build_feature_task` корректно использует `feature_description`.

**Brainstorm**: `docs/brainstorms/task-description-flow.md` (Option C)

**Файлы**:
- `services/langgraph/src/po/tools.py` — `trigger_engineering`
- `services/langgraph/src/nodes/developer.py` — `_build_create_task`
- `services/langgraph/src/po/prompts.py` — PO system prompt

## Steps

1. [ ] Persist description to DB in `trigger_engineering`
   - **Input**: `services/langgraph/src/po/tools.py:180-235` — `trigger_engineering` function
   - **Output**: After creating the task (POST /api/tasks/), if `description` is not empty AND `action == "create"`: PATCH `/api/projects/{project_id}` to merge description into `config.detailed_spec`. Use the existing `api.patch()` pattern — read current config, merge `detailed_spec`, write back. This ensures description survives queue consumption and is available on retry.
   - **Test**: Unit test in `services/langgraph/tests/unit/po/test_tools.py` — mock API calls, verify PATCH is called with `config.detailed_spec` when action=create and description is provided. Verify NO patch when action=feature (feature doesn't need persistence — it's already in the queue message and used directly). Verify NO patch when description is None.

2. [ ] Use `feature_description` as fallback in `_build_create_task`
   - **Input**: `services/langgraph/src/nodes/developer.py:393-446` — `_build_create_task` method
   - **Output**: Add `feature_description: str | None = None` parameter to `_build_create_task`. In the template, replace `{project_spec.get("detailed_spec", "N/A")}` with logic: use `project_spec.detailed_spec` if present and not empty, else use `feature_description` if present, else "N/A". Also update `_build_task_message` (line 340) to pass `feature_description` to `_build_create_task` (currently only passed to `_build_feature_task`).
   - **Test**: Unit test in `services/langgraph/tests/unit/nodes/test_developer.py` — test `_build_create_task` with: (a) detailed_spec in project_spec → uses it, (b) no detailed_spec but feature_description → uses feature_description, (c) neither → "N/A". Test `_build_task_message` for action=create passes feature_description through.

3. [ ] Update PO prompt to pass detailed_spec in create_project
   - **Input**: `services/langgraph/src/po/prompts.py` — step 5-6 in "Scenario: User Wants to Create a NEW Bot/Project"
   - **Output**: Update step 5 to instruct PO: "Pass the gathered description as `detailed_spec` to `create_project`". Update step 6 to clarify: "Also pass the same description to `trigger_engineering(description=...)` for immediate use". This is a prompt-level change — it instructs the LLM to fill both fields, providing defense in depth alongside the code fixes in steps 1-2.
   - **Test**: No code test (prompt change). Manual verification: trigger a new project creation via Telegram, check that `config.detailed_spec` is populated in DB and TASK.md contains the full description.

4. [ ] Update brainstorm status and verify end-to-end
   - **Input**: `docs/brainstorms/task-description-flow.md`
   - **Output**: Set brainstorm status to `done`. Run `make test-langgraph-unit` to verify all new tests pass. Verify existing tests still pass.
   - **Test**: `make test-langgraph-unit`

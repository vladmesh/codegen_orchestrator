# Feature branches for stories: engineering consumer creates story branch, workers push there

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Currently all worker commits go directly to main. There are no feature branches.
The CI gate polls GitHub Actions after each push to main, consuming CI minutes
on every intermediate push. This task introduces story-level feature branches:
engineering consumer creates `story/{story_id}` branch before the first task,
workers push to that branch. Main stays clean.

Source: docs/brainstorms/worker-context-architecture.md — CI Architecture section.
Blocked by: nothing. Blocks: task-1485ea29 (PR-based CI gate).

### Current flow
- Scaffolder → push to main (scaffold.py line 144)
- Worker manager → `_refresh_git_token` sets remote URL, workspace is on main
- INSTRUCTIONS.md → "commit, do NOT push unless task tells you"
- Worker wrapper → detects commit SHA via HEAD diff
- Engineering consumer → no branch logic, passes to developer node as-is

### What needs to change
1. EngineeringMessage gets a `branch` field
2. Engineering consumer resolves the branch name before spawning
3. Worker manager creates the branch in the workspace (or checks it out if exists)
4. INSTRUCTIONS.md updated to mention pushing to the story branch
5. Worker wrapper reports the branch in output
6. SpawnResult already has `branch` field — just needs to be populated

## Steps

1. [ ] Add `branch` field to EngineeringMessage contract
   - **Input**: `shared/contracts/queues/engineering.py`
   - **Output**: `EngineeringMessage.branch: str | None = None` field added
   - **Test**: Unit test that EngineeringMessage serializes/deserializes with branch field
   ⚠️ needs-approval (shared/contracts change)

2. [ ] Engineering consumer: resolve and pass story branch name
   - **Input**: `services/langgraph/src/consumers/engineering.py`
   - **Output**: Before building subgraph_input, compute `branch = f"story/{story_id}"` when story_id is present. Pass branch through to subgraph state. For standalone tasks (no story_id), branch stays None (push to main).
   - **Test**: Unit test: process_engineering_job with story_id → branch in subgraph_input; without story_id → branch is None

3. [ ] Add `branch` to EngineeringState and pass through to developer node
   - **Input**: `services/langgraph/src/subgraphs/engineering.py` (state definition), `services/langgraph/src/nodes/developer.py`
   - **Output**: EngineeringState gets `branch: str | None`. Developer node reads `state["branch"]` and passes to request_spawn/send_task_to_worker.
   - **Test**: Unit test: developer node passes branch to spawn call

4. [ ] Worker spawner: pass branch to CreateWorkerCommand and task messages
   - **Input**: `services/langgraph/src/clients/worker_spawner.py`, `shared/contracts/queues/worker.py`
   - **Output**: `request_spawn()` and `send_task_to_worker()` accept `branch` param. WorkerConfig gets `branch: str | None = None`. Branch passed in CreateWorkerCommand and in task_message dict.
   - **Test**: Unit test: request_spawn with branch → CreateWorkerCommand includes branch; task_message includes branch
   ⚠️ needs-approval (shared/contracts change — WorkerConfig)

5. [ ] Worker manager: checkout story branch after container creation
   - **Input**: `services/worker-manager/src/manager.py`, `services/worker-manager/src/consumer.py`
   - **Output**: In `create_worker_with_capabilities`, after git token refresh, if `branch` is provided: `git checkout -b {branch} || git checkout {branch}` (create or switch). For reused workers (send_task_to_worker), branch is already checked out — no action needed.
   - **Test**: Unit test: create_worker_with_capabilities with branch → exec_in_container called with git checkout command; without branch → no checkout exec

6. [ ] Worker wrapper: include branch in output and handle push to branch
   - **Input**: `packages/worker-wrapper/src/worker_wrapper/wrapper.py`
   - **Output**: After extracting git SHA, also extract current branch name (`git rev-parse --abbrev-ref HEAD`). Include `branch` field in result dict. Worker wrapper doesn't need to change push logic — the agent pushes to whatever branch is checked out.
   - **Test**: Unit test: _extract_branch returns current branch name; result dict includes branch

7. [ ] INSTRUCTIONS.md: update push/commit guidance for story branches
   - **Input**: `services/langgraph/src/prompts/developer_worker/INSTRUCTIONS.md`
   - **Output**: Update step 8 and Commit section: "After tests pass, commit and push your changes. The workspace is on the correct branch — just `git push`." Remove "do NOT push" caveat — with feature branches, pushing is safe and expected on every task.
   - **Test**: Read INSTRUCTIONS.md, verify push instruction is present

8. [ ] Task dispatcher: pass branch name in EngineeringMessage
   - **Input**: `services/scheduler/src/tasks/task_dispatcher.py`
   - **Output**: In `dispatch_todo_tasks`, when building EngineeringMessage, compute branch from story_id: `branch=f"story/{story_id}" if story_id else None`. Add to EngineeringMessage constructor.
   - **Test**: Unit test: dispatch_todo_tasks with story_id → EngineeringMessage has branch; without → None

9. [ ] Integration test: full branch flow
   - **Input**: Tests from steps 2-8
   - **Output**: Integration test that verifies: engineering consumer with story_id → branch resolved → passed to developer node → passed to spawn → worker manager creates branch → wrapper reports branch. Mock-based, no real containers.
   - **Test**: Integration test in `services/langgraph/tests/integration/`


# Architect node — story decomposition into tasks + task dispatcher

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Transition from 1 story = 1 engineering run to story → tasks → runs with automatic decomposition.
Currently `create_story` (PO tool) creates a Story + 1 Run → publishes directly to `engineering:queue`.
The architect node introduces an async decomposition step: story → architect:queue → architect consumer (LLM) → N tasks with dependencies → task dispatcher → engineering runs.

Ref: brainstorm bs-4fc78a0e (architect-node-orchestration.md)

**Current state**:
- Task model has `story_id`, `blocked_by_task_id`, `status` lifecycle — ready
- Run model has `task_id` FK — ready
- Scheduler has Redis consumer pattern (`provisioner_results_worker`) — reusable for architect
- PO consumer has concurrent processing pattern (`create_task` + semaphore) — reusable
- Task events API exists (`GET/POST /api/tasks/{id}/events`) — reusable for cumulative context
- Worker-manager handles container lifecycle via `worker:commands` queue
- No architect queue/message/consumer exists yet
- Engineering worker updates Run status but NOT Task status

**Architecture decisions**:
1. Architect runs as a consumer loop inside scheduler service (not a separate container). Pure I/O (API + LLM calls). Concurrent processing via `asyncio.create_task()` + `Semaphore(5)`.
2. **Reuse worker per story** — spawn worker container once for the first task, reuse for subsequent tasks in the same story. Kill on story complete. Eliminates spawn + clone overhead on tasks 2..N.
3. **Skip deploy per task** — deploy triggers only on story complete (all tasks done), not after each engineering run.
4. **Cumulative context via task events** — engineering worker writes `iteration_end` events with file changes and results. Task dispatcher aggregates sibling task events and injects context into next task description.
5. **API stays pure CRUD** — all orchestration logic (story completion, deploy trigger, worker cleanup) lives in scheduler/dispatcher, event-driven. API is a thin DB layer, nothing more.

## Steps

1. [ ] ArchitectMessage contract + queue constants
   - **Input**: `shared/contracts/queues/engineering.py` (pattern), `shared/queues.py`, `shared/contracts/base.py`
   - **Output**: `shared/contracts/queues/architect.py` with `ArchitectMessage(story_id, project_id, user_id)`. Add `ARCHITECT_QUEUE = "architect:queue"` and `ARCHITECT_GROUP = "architect-workers"` to `shared/queues.py`. Add `QueueBinding` to `QUEUE_TOPOLOGY`.
   - **Test**: Unit test validates ArchitectMessage serialization, queue constants exist, topology includes architect binding

2. [ ] Architect consumer in scheduler — concurrent LLM decomposition
   - **Input**: `services/scheduler/src/main.py` (consumer pattern from `provisioner_results_worker`), `services/langgraph/src/po/consumer.py` (concurrency pattern with semaphore)
   - **Output**: `services/scheduler/src/tasks/architect_consumer.py` — consumes `architect:queue` with concurrent processing (`create_task` + `Semaphore(5)`). For each message: fetches story + project config + existing tasks via API, calls LLM to decompose story into tasks (structured output), creates tasks via API with `story_id`, `status=todo`, and `blocked_by_task_id` chains. Register consumer loop in `scheduler/src/main.py`.
   - **Test**: Unit test with mocked API + LLM: verify task creation, dependency chains, story_id linkage, concurrent processing. Test error handling.

3. [ ] Modify create_story PO tool — publish to architect:queue
   - **Input**: `services/langgraph/src/po/tools.py` (`create_story` function, lines 177-288)
   - **Output**: Replace Run creation + engineering:queue publish with architect:queue publish. create_story now: (1) creates Story, (2) transitions to in_progress, (3) publishes ArchitectMessage to architect:queue, (4) persists detailed_spec. No Run created here — dispatcher handles that.
   - **Test**: Unit test: mock API + Redis, verify ArchitectMessage published, no Run created, story transitioned. ⚠️ Changes PO tool contract.

4. [ ] Engineering worker — task status updates + event writing, no deploy
   - **Input**: `services/langgraph/src/consumers/engineering.py`, task events API
   - **Output**: When run has linked `task_id`: (1) update task status alongside run status (in_dev → in_ci → done/failed via task API), (2) on completion write `iteration_end` event with details: `{files_changed, commit_sha, summary, ci_result}`, (3) **skip deploy trigger** — do not publish to deploy:queue (deploy moves to dispatcher on story complete). Graceful: skip task updates if no task_id linked (backward compat — old runs without task_id still trigger deploy as before).
   - **Test**: Unit test: mock API, verify task transitions + event written + no deploy when task_id present. Test backward compat (no task_id → old behavior with deploy).

5. [ ] Task Dispatcher — worker reuse + cumulative context + story completion
   - **Input**: `services/scheduler/src/main.py`, worker-manager API, task events API, deploy queue
   - **Output**: `services/scheduler/src/tasks/task_dispatcher.py` — polls every 30s. Two responsibilities:
     **A) Dispatch todo tasks**: find tasks with `status=todo` where blocker is null or done. For each:
       (a) Cumulative context: fetch events of completed sibling tasks (same story_id), build context summary, prepend to task description in EngineeringMessage.
       (b) Worker reuse: check if a live worker exists for this story (via run_metadata of previous story runs). If yes → pass worker_id in EngineeringMessage. If no → engineering worker spawns new one.
       (c) Create Run (with task_id FK), publish EngineeringMessage, transition task to in_dev.
     **B) Complete stories**: find stories where status=in_progress and all linked tasks are done. For each:
       (a) Transition story to completed via API.
       (b) Trigger deploy: publish DeployMessage to deploy:queue.
       (c) Notify PO: publish to po:proactive.
       (d) Clean up: kill story worker via worker:commands.
   Register as scheduler worker in main.py.
   - **Test**: Unit tests: (A) unblocked tasks get runs, blocked skipped, context aggregation, worker_id reuse. (B) all-done story → completed + deploy + notify + worker killed. Partial-done story → no action.

6. [ ] Integration test — full architect pipeline
   - **Input**: All components from steps 1-5
   - **Output**: Integration test: create story → architect:queue → architect creates tasks → dispatcher picks up first task (spawns worker) → engineering completes + writes event → dispatcher picks up second task (reuses worker, injects context) → all tasks done → dispatcher completes story + triggers deploy + kills worker.
   - **Test**: End-to-end flow with real Redis + DB, mocked LLM + worker execution.



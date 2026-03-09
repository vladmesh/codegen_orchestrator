# Worker reuse per story — spawn once, reuse for subsequent tasks

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Brainstorm Gap 4 (bs-e2a5e8c3): each task in a story spawns a new worker container + git clone (~50s overhead). For a 5-task story that's 5x wasted time.

### Current flow
1. Dispatcher finds todo task → creates Run → publishes `EngineeringMessage` to `engineering:queue`
2. Engineering consumer → `process_engineering_job()` → engineering subgraph → `DeveloperNode.run()` → `request_spawn()` → new container
3. After CI gate → `delete_worker(worker_id, reason="completed")`
4. Next task dispatched → repeat from step 1

### Target flow
1. First task: spawn worker, store `worker_id` in Redis keyed by `story_id`
2. Subsequent tasks: look up worker_id → `send_task_to_worker()` instead of `request_spawn()`
3. Story complete/failed: cleanup worker

### Key insight
The worker reuse must happen at the **engineering consumer** level (not dispatcher), because:
- Dispatcher doesn't know about workers — it only creates Runs and publishes messages
- Engineering consumer already has `worker_id` from `SpawnResult` and `send_task_to_worker()` for CI fix reuse
- The pattern to extend: instead of deleting worker after each task, keep it alive if story has more tasks

### Storage for worker_id per story
Use Redis hash `story:workers` mapping `story_id → worker_id`. Engineering consumer writes it after first spawn, reads it for subsequent tasks. Scheduler cleans it on story complete/fail.

## Steps

1. [ ] Add `story_id` field to `EngineeringMessage` contract ⚠️ needs-approval
   - **Input**: `shared/contracts/queues/engineering.py`, `services/scheduler/src/tasks/task_dispatcher.py`
   - **Output**: `EngineeringMessage` gains `story_id: str | None = None`. Dispatcher passes `story_id` when dispatching story tasks.
   - **Test**: Unit test: EngineeringMessage with/without story_id serializes correctly. Dispatcher test: verify story_id is included in published message.

2. [ ] Story worker registry — Redis helper to store/lookup/cleanup worker_id per story
   - **Input**: `services/langgraph/src/clients/worker_spawner.py`
   - **Output**: Three new functions: `get_story_worker(story_id) → str|None`, `set_story_worker(story_id, worker_id)`, `clear_story_worker(story_id)`. Uses Redis hash `story:workers`.
   - **Test**: Unit tests for all three functions using mock Redis.

3. [ ] Engineering consumer: reuse worker for story tasks (skip delete between tasks)
   - **Input**: `services/langgraph/src/consumers/engineering.py`, `services/langgraph/src/nodes/developer.py`, `services/langgraph/src/clients/worker_spawner.py`
   - **Output**: 
     - `process_engineering_job()`: if `story_id` set, check `get_story_worker()` for existing worker. Pass `worker_id` into subgraph input if found.
     - `DeveloperNode.run()`: if `worker_id` provided in state, use `send_task_to_worker()` instead of `request_spawn()`. Fall back to `request_spawn()` if worker is dead.
     - After subgraph: if story_id set, call `set_story_worker()` instead of deleting worker. If no story_id (standalone run), delete as before.
   - **Test**: Unit tests: (a) first task in story spawns + stores worker_id, (b) second task reuses worker_id, (c) dead worker falls back to respawn, (d) standalone task (no story_id) deletes worker as before.

4. [ ] Story completion/failure: cleanup story worker
   - **Input**: `services/scheduler/src/tasks/task_dispatcher.py`, `services/langgraph/src/clients/worker_spawner.py`
   - **Output**: 
     - `complete_stories()`: after completing story, call `clear_story_worker(story_id)` + `delete_worker(worker_id)` if worker exists.
     - `supervise_failed_tasks()`: when failing a story (terminal failure), also cleanup story worker.
     - Import `delete_worker` and story worker registry in scheduler (or expose via shared helper).
   - **Test**: Unit tests: (a) story complete cleans up worker, (b) story failure cleans up worker, (c) no-op when no story worker exists.

5. [ ] Integration test — full story lifecycle with worker reuse
   - **Input**: All modified files
   - **Output**: Integration test that simulates: story with 2 tasks → first task dispatched → worker spawned + stored → first task completes → second task dispatched → worker reused → second task completes → story completed → worker cleaned up.
   - **Test**: Integration test using mocked Redis + API client, verifying the full flow end-to-end.


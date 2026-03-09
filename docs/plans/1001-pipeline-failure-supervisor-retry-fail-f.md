# #1001 Pipeline failure supervisor — retry, fail-fast, admin logging

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

The task dispatcher (services/scheduler/src/tasks/task_dispatcher.py) currently does two things:
dispatch todo tasks and complete stories when all tasks are done. There is NO failure
handling — if a story gets stuck in `created` (architect didn't run), a task fails, or a
worker hangs in `in_dev`, the pipeline silently stalls.

The Task model already has `current_iteration` (default 0) and `max_iterations` (default 3)
fields, but they are unused. The supervisor will leverage these for retry tracking.

Key gaps to address:
- StoryStatus has no `FAILED` state — need to add it
- TaskUpdate schema doesn't include `current_iteration` — need to add it
- Scheduler API client has no `update_task` method — need to add it
- Story API has no `fail` endpoint — need to add it
- No supervisor logic exists anywhere

Source: brainstorm bs-e2a5e8c3 (recovery strategy discussion).

## Steps

1. [ ] Add StoryStatus.FAILED and story fail endpoint
   - **Input**: `shared/contracts/dto/story.py`, `services/api/src/routers/stories.py`
   - **Output**: StoryStatus.FAILED enum value, VALID_TRANSITIONS updated (IN_PROGRESS→FAILED, CREATED→FAILED), `POST /stories/{id}/fail` endpoint
   - **Test**: Unit test: valid transitions include FAILED; API test: fail endpoint returns 200 and updates status
   - ⚠️ needs-approval (changes shared/contracts/)

2. [ ] Add current_iteration to TaskUpdate schema + update_task to scheduler API client
   - **Input**: `services/api/src/schemas/task.py`, `services/scheduler/src/clients/api.py`
   - **Output**: TaskUpdate.current_iteration field (optional int), SchedulerAPIClient.update_task() method, SchedulerAPIClient.fail_story() method
   - **Test**: Unit test: TaskUpdate accepts current_iteration; API client method constructs correct PATCH request

3. [ ] Implement supervise_stuck_stories (story in created > N minutes)
   - **Input**: `services/scheduler/src/tasks/task_dispatcher.py`
   - **Output**: New function `supervise_stuck_stories()` — finds stories in `created` status with `created_at` older than threshold (5 min default). For each: check retry count via task events (note events with `supervisor_retry`), if < 3 retries → republish ArchitectMessage to architect:queue + log warning. If >= 3 → fail story + log error.
   - **Test**: Unit tests: story stuck → republish; story stuck past retries → fail; story not stuck → skip

4. [ ] Implement supervise_failed_tasks (task in failed → retry or escalate)
   - **Input**: `services/scheduler/src/tasks/task_dispatcher.py`
   - **Output**: New function `supervise_failed_tasks()` — finds tasks with `failed` status and `story_id` set. For each: if `current_iteration < max_iterations` → transition failed→backlog→todo, increment current_iteration, log warning. If `current_iteration >= max_iterations` → fail all remaining sibling tasks → fail story, log error.
   - **Test**: Unit tests: failed task retried (transitions + iteration bump); failed task exhausted retries → story failed; failed task without story_id → skip

5. [ ] Implement supervise_stuck_tasks (task in in_dev > M minutes)
   - **Input**: `services/scheduler/src/tasks/task_dispatcher.py`
   - **Output**: New function `supervise_stuck_tasks()` — finds tasks with `in_dev` status, checks `updated_at` age (30 min default). If stuck → transition to `failed` + log warning. The failed task will be picked up by `supervise_failed_tasks` for retry.
   - **Test**: Unit tests: stuck in_dev task → failed transition; recent in_dev task → skip

6. [ ] Integrate supervisor into dispatcher loop + PO notification on terminal failure
   - **Input**: `services/scheduler/src/tasks/task_dispatcher.py`
   - **Output**: `task_dispatcher_loop()` calls all 3 supervisor functions each cycle (after dispatch + complete). On any story failure (terminal), publish POProactiveMessage to notify user. Add configurable thresholds as module constants.
   - **Test**: Unit test: loop calls supervisor functions; PO notification sent on story failure

7. [ ] Integration test — full supervisor cycle
   - **Input**: `services/scheduler/tests/integration/`
   - **Output**: Integration test with mock API + Redis verifying: stuck story → retry → architect queue message; failed task → reopen → todo; stuck in_dev → fail → reopen; terminal failure → story failed + PO notification
   - **Test**: Integration test covering the full supervisor cycle end-to-end


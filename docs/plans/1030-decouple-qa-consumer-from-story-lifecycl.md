# #1030 Decouple QA consumer from story lifecycle

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Same anti-pattern as #1006 (deploy): QA consumer directly manages story transitions (`_transition_story_safe`) and publishes user-facing events (`publish_story_event`). This couples the technical QA worker to business logic.

**Additional complication**: Unlike deploy, the QA consumer has no run tracking at all — no `RunType.QA`, no run creation/updates. The deploy pattern works because the dispatcher pre-creates a run and includes `task_id` in the message, and the consumer patches `runs/{task_id}` with the outcome. QA needs the same infrastructure.

**Current state:**
- `services/langgraph/src/consumers/qa.py` — 4 story transitions (L113, L177, L209, L246), 2 user events (L179-183, L210-218)
- `shared/contracts/queues/qa.py` — `QAMessage` has no `run_id` field
- `shared/contracts/dto/run.py` — `RunType` has only ENGINEERING, DEPLOY
- `services/scheduler/src/tasks/supervisor.py` — no `supervise_testing_stories()`

## Steps

1. [ ] Add QAOutcome enum + run_id to QA contracts
   - **Input**: `shared/contracts/queues/qa.py`, `shared/contracts/dto/run.py`
   - **Output**: `QAOutcome(StrEnum)` with PASSED, FAILED, EXHAUSTED, ERROR. `RunType.QA` added. `QAMessage.run_id: str` field added.
   - **Test**: Unit test — QAOutcome values serialize correctly, QAMessage accepts run_id

2. [ ] Strip story lifecycle from QA consumer
   - **Input**: `services/langgraph/src/consumers/qa.py`
   - **Output**: Remove `_transition_story_safe`, remove `publish_story_event` import/calls. `_handle_qa_pass` and `_handle_qa_fail` now only patch `runs/{run_id}` with `qa_outcome` + details (same pattern as `deploy_result_handler.py`). `process_qa_job` reads `msg.run_id` for run patching.
   - **Test**: Unit test — `process_qa_job` patches run with correct outcome, does NOT call `transition_story`

3. [ ] Create QA run in dispatcher before publishing QAMessage
   - **Input**: `services/scheduler/src/tasks/supervisor.py` (`_handle_deploy_success_story`)
   - **Output**: `_handle_deploy_success_story` creates a QA run (`RunType.QA`) via `api_client.create_run()` and passes `run_id` in `QAMessage`. Matches the pattern where deploy runs are pre-created before message publish.
   - **Test**: Unit test — `_handle_deploy_success_story` creates QA run and includes run_id in published QAMessage

4. [ ] Add supervise_testing_stories() to supervisor
   - **Input**: `services/scheduler/src/tasks/supervisor.py`
   - **Output**: New function `supervise_testing_stories()` — polls TESTING stories, reads latest QA run's `result.qa_outcome`, routes:
     - PASSED → story completed, publish `story_completed` event to PO
     - FAILED → increment qa_attempt, re-publish QAMessage if under MAX_QA_LOOPS, create fix task + redispatch to engineering
     - EXHAUSTED → story failed, publish `story_failed` event to PO
     - ERROR → story failed (same as EXHAUSTED for now)
   - **Test**: Unit tests for each outcome branch (4 tests minimum), plus skip-if-still-running

5. [ ] Wire supervise_testing_stories into dispatcher loop
   - **Input**: `services/scheduler/src/tasks/task_dispatcher.py`
   - **Output**: Import and call `supervise_testing_stories` in the main loop, add result to cycle summary log. Update `__all__` exports.
   - **Test**: Unit test — dispatcher loop calls supervise_testing_stories

6. [ ] Integration test — QA pass/fail through dispatcher
   - **Input**: All modified files
   - **Output**: Service test that verifies: QA consumer stores outcome in run → dispatcher reads it → story transitions correctly. Cover PASSED and FAILED paths.
   - **Test**: Service test with real DB/Redis


# Add TESTING status to StoryStatus + API transition endpoint + QA queue contract

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Brainstorm bs-eece61a8 (docs/brainstorms/post-release-qa-mvp.md) — post-release QA via Claude Code on prod servers.
This is the foundational task: add the TESTING status, API endpoint, and queue contract so subsequent tasks can wire up the QA consumer and deploy consumer changes.

**Current state**:
- `StoryStatus` has 9 values, status stored as `String(50)` in DB (no DB enum → no migration needed)
- Story transitions enforced by `VALID_TRANSITIONS` dict and `_do_transition()` in stories router
- Queue contracts follow `BaseMessage`/`BaseResult` pattern in `shared/contracts/queues/`
- Queue constants + topology in `shared/queues.py`
- Existing tests: `services/api/tests/unit/test_story_dto.py` (23 tests), `test_story_model.py`

## Steps

1. [ ] Add TESTING to StoryStatus enum + transitions
   - **Input**: `shared/contracts/dto/story.py`
   - **Output**: `StoryStatus.TESTING = "testing"`, transitions: `DEPLOYING → {TESTING, ...}`, `TESTING → {COMPLETED, IN_PROGRESS, FAILED}`
   - **Test**: Update `test_story_dto.py` — membership count 9→10, new transition tests: deploying→testing, testing→completed, testing→in_progress, testing→failed, testing has entry in VALID_TRANSITIONS

2. [ ] Add POST /stories/{id}/test endpoint
   - **Input**: `services/api/src/routers/stories.py`
   - **Output**: New `test_story()` endpoint following the pattern of `deploy_story()` — transitions to TESTING
   - **Test**: `services/api/tests/unit/test_story_model.py` — add test for /test endpoint (mock DB, verify transition)

3. [ ] Create QAMessage contract
   - **Input**: `shared/contracts/base.py` (BaseMessage pattern), `shared/contracts/queues/deploy.py` (reference)
   - **Output**: New `shared/contracts/queues/qa.py` with `QAMessage(BaseMessage)` containing: story_id, project_id, user_id, deployed_url, server_ip (optional, QA consumer can fetch), qa_attempt (int, default 0)
   - **Test**: New `shared/tests/unit/test_qa_contract.py` — validate QAMessage construction, defaults, serialization

4. [ ] Add QA_QUEUE constant + topology
   - **Input**: `shared/queues.py`
   - **Output**: `QA_QUEUE = "qa:queue"`, `QA_GROUP = "qa-consumers"`, new `QueueBinding` in `QUEUE_TOPOLOGY`
   - **Test**: Existing `test_all_statuses_have_transitions` parametrized pattern — add similar check that QA_QUEUE is in topology (or simple assertion in qa contract test file)


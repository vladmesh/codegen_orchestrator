# Fix deploy failure classification and worker rejection pipeline

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

E2E test (weather_bot, 2026-03-16) revealed that a deploy failure caused by port conflict was incorrectly sent to an engineering worker as a code fix task. Two lines of defense failed:

1. **Deploy classifier LLM call crashed** — model ID `anthropic/claude-haiku-4-5-20251001` is invalid on OpenRouter. Fallback is hardcoded to `CODE`, so every classification failure = send to engineering worker.
2. **Worker rejection pipeline is disconnected** — `reject_reason` is parsed correctly by result_parser, carried in SpawnResult, but DeveloperNode ignores it. Engineering consumer never routes to `_handle_worker_reject()` (dead code path — engineering_status `worker_rejected` is never set).

Current state:
- `services/langgraph/src/consumers/deploy.py`: binary CODE/INFRA classifier, fallback=CODE, model ID broken
- `services/langgraph/src/nodes/developer.py`: no check for `worker_result.reject_reason`
- `services/langgraph/src/subgraphs/engineering.py`: no `worker_rejected` status in routing
- `services/langgraph/src/consumers/engineering.py`: `_handle_worker_reject()` exists but is never called
- `services/langgraph/src/prompts/developer_worker/INSTRUCTIONS.md`: reject guidance buried deep

## Steps

1. [ ] Fix classifier model ID and add three-way classification
   - **Input**: `services/langgraph/src/consumers/deploy.py` (lines 39-86)
   - **Output**: 
     - Model ID fixed to `anthropic/claude-haiku-4-5` (valid OpenRouter ID)
     - CLASSIFY_PROMPT updated for three categories: `RETRY`, `CODE_FIX`, `GIVE_UP`
     - `_classify_deploy_failure()` returns one of three values
     - Fallback on LLM failure changed from `CODE` → `RETRY` (safer default)
   - **Test**: Unit test calling classifier with known error strings: "port already allocated" → GIVE_UP, "ImportError" → CODE_FIX, "SSH timeout" → RETRY. Test LLM failure fallback = RETRY.

2. [ ] Update deploy consumer routing for three-way classification
   - **Input**: `services/langgraph/src/consumers/deploy.py` (lines 158-741 — `_redispatch_to_engineering`, `_handle_smoke_failure`, `process_deploy_job`)
   - **Output**: 
     - Classification routing: RETRY → `_handle_deploy_failure` (existing retry counter), CODE_FIX → `_redispatch_to_engineering`, GIVE_UP → mark story as failed + notify admin (HITL)
     - New `_handle_give_up()` function that fails story, deletes worker, notifies admin
     - Both smoke failure and deploy failure paths updated
   - **Test**: Unit test for each classification path: RETRY triggers retry counter, CODE_FIX triggers engineering redispatch, GIVE_UP triggers story failure + admin notification.

3. [ ] Wire up worker rejection in DeveloperNode
   - **Input**: `services/langgraph/src/nodes/developer.py` (lines 210-281), `services/langgraph/src/subgraphs/engineering.py`
   - **Output**: 
     - DeveloperNode checks `worker_result.reject_reason` after spawn/task result
     - If reject_reason is set, returns `engineering_status: "worker_rejected"` with `reject_reason` in state
     - EngineeringState gets `reject_reason: str | None` field
     - `route_after_developer` handles `worker_rejected` status (routes to blocked)
   - **Test**: Unit test: mock SpawnResult with reject_reason → DeveloperNode returns worker_rejected status.

4. [ ] Route worker_rejected in engineering consumer
   - **Input**: `services/langgraph/src/consumers/engineering.py` (lines 598-648)
   - **Output**: 
     - Add `elif result.get("engineering_status") == "worker_rejected":` branch in `process_engineering_job`
     - Extract `reject_reason` from result, call existing `_handle_worker_reject()`
   - **Test**: Unit test: mock engineering subgraph returning worker_rejected → verify `_handle_worker_reject` is called with correct args.

5. [ ] Add reject-first sanity check to worker instructions
   - **Input**: `services/langgraph/src/prompts/developer_worker/INSTRUCTIONS.md`
   - **Output**: 
     - Add "Step 0: Sanity Check" section right after "Before You Start" and before "Workflow"
     - Content: Before any coding, check if the error/task is actually solvable by writing code. If it's infrastructure (port conflicts, SSH failures, disk space, DNS, missing secrets) → REJECT immediately using `orch reject --reason "..."`. If the task is fundamentally impossible → REJECT.
     - Move/reinforce rejection guidance to be prominent at the top of workflow
   - **Test**: Manual review — no automated test needed for prompt changes.

6. [ ] Integration test: deploy failure → classification → correct routing
   - **Input**: Steps 1-4 outputs
   - **Output**: Integration test in `services/langgraph/tests/unit/consumers/` that mocks the deploy subgraph failure and verifies the full flow: classify → route → correct handler called
   - **Test**: Test cases: port conflict → give_up (no engineering dispatch), import error → code_fix (engineering dispatch), SSH timeout → retry (retry counter incremented)


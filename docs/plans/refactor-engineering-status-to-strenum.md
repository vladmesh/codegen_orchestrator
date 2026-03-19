# Refactor engineering_status to StrEnum

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

`engineering_status` is a bare `str` on `EngineeringState` with 6 undocumented values, 2 of which are dead (`working`, `developer_blocked`). The brainstorm "Worker Result API Unification" (2026-03-19) defines a binary worker outcome model: success → DONE, gave_up → WAITING_HUMAN, crash → FAILED (auto-retry) → GAVE_UP when retries exhausted.

This task creates `EngineeringStatus(StrEnum)` with 4 values (IDLE, DONE, GAVE_UP, FAILED), replaces all bare strings, and simplifies supervisor routing (no more NON_RETRYABLE_REASONS). `FailureReason` enum is not needed — reason is free text in `failure_metadata`.

### Value mapping

| Old | New | Rationale |
|-----|-----|-----------|
| `idle` | `IDLE` | Same — initial state |
| `working` | *(removed)* | Never set anywhere |
| `done` | `DONE` | Same — success with commit |
| `blocked` (generic crash) | `FAILED` | Technical failure, auto-retry |
| `blocked` (has block_reason) | `GAVE_UP` | Worker explicitly couldn't complete |
| `worker_rejected` | `GAVE_UP` | Worker explicitly couldn't complete |
| `developer_blocked` | *(never existed in code)* | Was a bug — replaced by GAVE_UP |

### FAILED lifecycle

FAILED is transient — supervisor either retries (→ IDLE) or exhausts retries (→ GAVE_UP). Human never sees a stuck FAILED task.

## Steps

1. [ ] Create `EngineeringStatus(StrEnum)` in shared/contracts
   - **Input**: value mapping above
   - **Output**: `shared/contracts/dto/engineering.py` with enum, re-exported from `__init__.py`
   - **Test**: Unit test — enum values are correct strings, StrEnum equality with bare strings works

2. [ ] Replace bare strings in engineering subgraph (state + nodes + router)
   - **Input**: `services/langgraph/src/subgraphs/engineering.py`
   - **Output**: `EngineeringState.engineering_status` annotated as `EngineeringStatus`. DoneNode returns `DONE`, BlockedNode returns `GAVE_UP` (was `blocked`). `route_after_developer`: `GAVE_UP` and `FAILED` → "blocked" route (node name stays, just the status values change).
   - **Test**: Existing subgraph node tests pass with updated assertions

3. [ ] Replace bare strings in developer node — fix the blocked/gave_up split
   - **Input**: `services/langgraph/src/nodes/developer.py`
   - **Output**: 
     - success + no commit → `FAILED` (technical, retryable)
     - success + commit → `DONE`
     - reject_reason → `GAVE_UP`
     - block_reason → `GAVE_UP` (was `blocked`, this is the bug fix)
     - generic error → `FAILED` (technical, retryable)
   - **Test**: `test_developer_node.py` — verify each path returns correct enum value

4. [ ] Replace bare strings in engineering consumer — simplify routing
   - **Input**: `services/langgraph/src/consumers/engineering.py`
   - **Output**: Init uses `IDLE`. Result routing: `DONE` → handle_success, `GAVE_UP` → handle_worker_gave_up (merge old blocked+reject paths), `FAILED` → handle_technical_failure (new, or reuse fail_job). Remove separate `_handle_worker_blocked` / `_handle_worker_reject` distinction — one handler.
   - **Test**: `test_engineering_blocked.py`, `test_engineering_reject.py` updated

5. [ ] Simplify engineering_result_handler — merge blocked+reject handlers
   - **Input**: `services/langgraph/src/consumers/engineering_result_handler.py`
   - **Output**: `handle_worker_gave_up(reason)` replaces `handle_worker_blocked(block_reason)` + `handle_worker_reject(reject_reason)`. Task → WAITING_HUMAN with `failure_metadata = {"reason": reason}`. Remove `failure_reason` field from metadata — not needed when status encodes semantics.
   - **Test**: Merged test covering gave_up path

6. [ ] Simplify supervisor — remove NON_RETRYABLE_REASONS
   - **Input**: `services/scheduler/src/tasks/supervisor.py`, `task_dispatcher.py`
   - **Output**: Supervisor queries only `FAILED` tasks. If retries left → retry. If exhausted → transition to `GAVE_UP` (not story failure). Remove `NON_RETRYABLE_REASONS`. Task dispatcher: check siblings for `GAVE_UP` instead of checking failure_metadata.
   - **Test**: `test_task_dispatcher.py`, supervisor tests updated
   ⚠️ needs-approval: changes supervisor retry semantics (exhausted retries → GAVE_UP instead of story FAILED)

7. [ ] Update all remaining tests to use enum imports
   - **Input**: All test files with bare engineering_status / failure_reason strings
   - **Output**: Tests import `EngineeringStatus`, assertions use enum members
   - **Test**: `make test-unit` passes clean


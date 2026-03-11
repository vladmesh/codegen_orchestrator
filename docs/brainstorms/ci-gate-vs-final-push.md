# CI Gate: One Push per Story

**Status**: Triaged
**Date**: 2026-03-11

## Problem

Two mechanisms duplicate CI verification:
1. Engineering worker CI gate — pushes + waits CI after EVERY task
2. `append_ci_check_task` — creates a separate CI task at end of story

Result: CI runs N times per story (wastes GitHub Actions minutes), and the CI task gets stuck in `backlog` forever (bug: no explicit `status: "todo"`).

## Requirements

1. CI runs exactly **once per story** — save GitHub Actions minutes
2. Each **task** gets its own **commit** — for tracking in git log
3. After each task, worker can run **local tests** (unit/smoke) — but no push
4. Story **must not progress** until CI is green
5. Worker **must see CI errors** and fix them in one feedback loop

## Decision

Keep the CI check task, fix the flow, remove per-task CI gate.

### Per-task flow (ordinary tasks)
- Worker: implement → commit → run local tests (unit/smoke)
- **No push, no CI gate**
- Worker prompt: "Commit your changes. Do NOT push to GitHub unless the task explicitly tells you to."

### CI check task (one per story, at the end)
- Created by `append_ci_check_task` after architect finishes
- **Fix**: create with `status: "todo"` (currently missing → defaults to `backlog`)
- Blocked by last architect task → dispatcher picks it up when blocker is done
- Worker receives task: push all accumulated commits → wait for CI → fix if fails → re-push
- Worker prompt for this task already says "Push to GitHub. Wait for CI. If CI fails, read logs, fix, and push again."

### Fallback: `is_last_task` flag
- Dispatcher adds `is_last_task: bool` to `EngineeringMessage`
- True when no other `todo`/`backlog` tasks remain in story
- Engineering worker: if `is_last_task` and task is NOT the CI task → still run push + CI gate as safety net
- Covers edge cases: CI task deleted, architect didn't create tasks, etc.

## Changes Required

### 1. Fix `append_ci_check_task` (architect.py)
- Add `"status": "todo"` to task creation dict
- This alone fixes the stuck-in-backlog bug

### 2. Engineering worker: remove per-task CI gate
- `services/langgraph/src/consumers/engineering.py` — skip push + `_wait_for_ci_and_fix` for ordinary tasks
- Only run CI gate when: task is CI check (`created_by: system`) OR `is_last_task` flag is set

### 3. Worker prompt: no push
- Update developer instructions to say: "Commit your work. Do NOT push unless the task explicitly asks you to."
- CI check task already has the right prompt ("Push to GitHub...")

### 4. `EngineeringMessage`: add `is_last_task`
- `shared/contracts/queues/engineering.py` — add `is_last_task: bool = False`
- Dispatcher sets it by checking remaining todo/backlog tasks in story

### 5. Dispatcher: set `is_last_task`
- `services/scheduler/src/tasks/task_dispatcher.py` — before publishing engineering message, query tasks for story, if no remaining todo/backlog → `is_last_task=True`

## File Map

| File | Change |
|------|--------|
| `services/langgraph/src/consumers/architect.py` | Fix status: "todo" in CI task |
| `services/langgraph/src/consumers/engineering.py` | Conditional CI gate |
| `shared/contracts/queues/engineering.py` | Add `is_last_task` field |
| `services/scheduler/src/tasks/task_dispatcher.py` | Set `is_last_task` |
| Worker prompt (in engineering consumer) | "Do not push" instruction |
| `services/langgraph/tests/unit/test_architect_consumer.py` | Update tests |

## Action Items

- Fix append_ci_check_task status + conditional CI gate + worker prompt → backlog #1004
- `is_last_task` fallback flag → rejected (YAGNI)
- ✅ **DONE**: CI-check task can succeed without commit (`allow_no_commit` flag in EngineeringState). Developer node and engineering consumer both respect it. 6 unit tests.

## Not in Scope

- Local smoke tests (docker compose up in worker) — separate effort
- Per-task push as configurable option — YAGNI for now

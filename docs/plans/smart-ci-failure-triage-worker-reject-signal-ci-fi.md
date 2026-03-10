# Smart CI failure triage: worker reject signal + CI-fix task template

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

CI gate sends ALL failures to the worker with a generic "fix CI" prompt, even when the issue is infrastructure (missing secrets, registry auth, Docker login). The worker can't signal "not my problem" — a missing commit looks identical to "couldn't fix it". This causes 5 wasted cycles (3 supervisor retries × 2 CI gate retries each).

Brainstorm: `docs/brainstorms/smart-ci-failure-triage.md` (commit e8142f3). This task implements Phase 2: worker reject signal, CI-fix task template, and pipeline halt on reject.

**Current state:**
- `_ci_gate.py`: binary infra/code classification via `_is_infra_failure()` markers. Generic `_build_ci_fix_prompt()`.
- `worker_wrapper/wrapper.py`: returns `{status, content, commit_sha}` — no reject concept.
- `SpawnResult` dataclass: `success: bool` — no reject reason field.
- `engineering.py`: CI fail → mark run failed → supervisor retries blindly.
- `TaskStatus.BLOCKED` already exists in `shared/contracts/dto/task.py` with valid transitions from `IN_DEV` and `IN_CI`.
- `StoryStatus.FAILED` exists — terminal. Used when retries exhausted.
- `notify_admins()` in `shared/notifications.py` — sends Telegram to `is_admin=true` users.
- `supervise_failed_tasks()` in task_dispatcher.py retries all failed tasks equally.

**Key design decision:** Worker reject = orchestrator bug, not a product issue. PO and user don't need to know about technical failures at task level. On reject: task → blocked, story → failed with metadata `{reason: "worker_rejected", reject_reason: "..."}`. Admin gets notified via `notify_admins()`. No new story statuses needed.

## Steps

1. [ ] Add `REJECTED` marker parsing to worker wrapper
   - **Input**: `packages/worker-wrapper/src/worker_wrapper/wrapper.py`, `result_parser.py`
   - **Output**: Wrapper detects `## REJECTED` section in agent stdout (the agent writes it inline, not to a file). Sets `status: "rejected"` + `reject_reason` in result dict. Parsing: find `## REJECTED` header, extract everything after it as reason text.
   - **Test**: Unit tests — wrapper returns rejected status when output contains REJECTED marker; returns normal status otherwise. Test reject_reason extraction from multiline output.

2. [ ] Add `reject_reason` to SpawnResult and propagate through worker_spawner
   - **Input**: `services/langgraph/src/clients/worker_spawner.py`
   - **Output**: `SpawnResult` gets `reject_reason: str | None` field. `request_spawn()` and `send_task_to_worker()` populate it from worker output when status is "rejected". `success` is `False` for rejected.
   - **Test**: Unit test — SpawnResult correctly carries reject_reason from worker output stream.

3. [ ] Create CI-fix TASK.md template with reject instructions
   - **Input**: `services/langgraph/src/consumers/_ci_gate.py` (`_build_ci_fix_prompt`)
   - **Output**: Replace generic prompt with structured template: job name, run URL, failed step, CI logs, and reject instructions ("If this is NOT a code issue — infrastructure, missing secrets, orchestrator bug — write `## REJECTED` followed by explanation. Do NOT make any commits."). Keep existing `failure_context` and `attempt` params, add `run_url` param.
   - **Test**: Unit test — prompt template includes reject instructions, CI log context, and structured format.

4. [ ] Handle `rejected` in CI gate → propagate to engineering consumer
   - **Input**: `services/langgraph/src/consumers/_ci_gate.py`, `engineering.py`
   - **Output**: CI gate: when worker returns rejected, stop retry loop immediately, return `(ci_passed=False, ci_attempts, rejected=True, reject_reason=...)`. Engineering consumer: if rejected → task transitions to `blocked` (not `failed`) → story transitions to `failed` with metadata `{failure_reason: "worker_rejected", reject_reason: "<text>", task_id: "<id>"}` → call `notify_admins(message, level="error")` with task ID + reject reason. Skip the normal supervisor retry path entirely.
   - **Test**: Unit tests — (a) rejected worker → task=blocked, story=failed with metadata; (b) `notify_admins` called with reject_reason; (c) non-rejected failures still follow existing retry path; (d) no `po:proactive` message sent for rejected tasks.

5. [ ] Improve CI gate transient failure pre-filter with backoff retry
   - **Input**: `services/langgraph/src/consumers/_ci_gate.py` (`_try_infra_rerun`, `_is_infra_failure`)
   - **Output**: Narrow `_INFRA_FAILURE_MARKERS` to only clearly transient cases (GH Actions runner unavailable, network timeout, rate limit). For these: retry with backoff 1m, 3m, 5m (3 attempts). Everything else (registry auth, missing secrets, docker login) goes to the worker with the new CI-fix template — let the worker diagnose.
   - **Test**: Unit tests — transient failures get multiple retries with backoff; non-transient markers removed from infra list and reach the worker.

6. [ ] Ensure task_dispatcher handles blocked tasks correctly
   - **Input**: `services/scheduler/src/tasks/task_dispatcher.py`
   - **Output**: `supervise_failed_tasks()` already only queries `status=failed` so blocked tasks are skipped. Add explicit log when blocked tasks exist in the story (for observability). Verify `dispatch_todo_tasks()` skips stories with blocked tasks (sibling dependency check).
   - **Test**: Unit tests — blocked tasks not retried, not dispatched. Story with a blocked task does not dispatch remaining todo siblings.

7. [ ] Integration tests for full reject flow
   - **Input**: All changed files from steps 1-6
   - **Output**: Integration test: mock worker returns rejected → verify: task=blocked, story=failed with metadata, `notify_admins` called, no supervisor retry, no PO proactive message.
   - **Test**: End-to-end integration test covering the full reject → halt → admin notify path.


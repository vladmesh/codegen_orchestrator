# #1021 Add TTL/cleanup for stale Redis queue messages

## Context

During the 2026-03-13 escort, 75 stale messages in `architect:queue` blocked a real story for hours. The consumer processed each stale message sequentially — fetching story, checking status, hitting 422 or skip — before reaching the fresh one.

### Root cause

Messages from cancelled/failed/completed stories stay in the stream. MAXLEN ~1000 only caps size, not age. When a consumer restarts or claims pending, it replays all of them. The architect consumer already has a staleness guard (lines 84-99 of `architect.py`), but the guard still costs one API call per stale message. At 75 messages × ~200ms per call = 15+ seconds of pure latency before processing a real message. With heavier operations (engineering, deploy), the impact is worse.

### Current state

| Consumer | Has staleness guard? | Notes |
|----------|---------------------|-------|
| Architect (`architect.py:84-99`) | **Yes** — checks story status, skips COMPLETED/ARCHIVED/FAILED/DEPLOYING | Works but N API calls on stale flood |
| Engineering (`_base.py`) | **No** — no guard in base worker loop; engineering.py jumps straight to work | Stale task_id → fails at `PATCH runs/{task_id}` |
| Deploy (`deploy.py`) | **No** — validates project but not run/story staleness | Stale run → lock acquired, wasted work |
| QA (`qa.py`) | **No** — validates application but not story staleness | Minor: QA messages are rarer |
| Scaffold (`consumer.py`) | **No** | Minor: scaffold messages are rare |
| PO (`po.py`) | **No** — PO messages are user-driven, no story_id to check | Not applicable: PO doesn't have staleness concept |

### What needs to change

1. **Trim stale messages before they reach consumers** — periodic XTRIM on task queues to remove old entries.
2. **Consumer-side guard in _base.py** — validate run status before calling process_fn, ACK+skip dead runs. This is the cheap, universal fix.
3. **Cleanup orphan `po:response:*` streams** — they accumulate when telegram bot times out or crashes before `redis.delete()`.

### What does NOT need to change

- PO consumer — no story_id, not stale-prone.
- MAXLEN ~1000 — already reasonable, no change needed.
- XAUTOCLAIM timeout (60s) — fine for crash recovery.

## Steps

1. [ ] Add consumer-side staleness guard in `_base.py`
   - **Input**: `services/langgraph/src/consumers/_base.py`, `shared/contracts/queues/` (message schemas)
   - **Output**: Before calling `process_fn`, check if the run exists and is not already terminal (COMPLETED/FAILED/CANCELLED). If stale → ACK + skip + log warning with `stale_message_skipped` event. The guard should:
     - Parse `task_id` from `msg.data` (present in EngineeringMessage, DeployMessage, QAMessage as the run ID)
     - If `task_id` is present → `GET /runs/{task_id}` → if status in {COMPLETED, FAILED, CANCELLED} → skip
     - If `task_id` is missing (architect messages use story_id) → skip the guard (architect already has its own)
     - If API call fails → proceed with processing (don't block on guard failure)
   - **Test**: Unit test in `services/langgraph/tests/unit/test_base_worker.py`:
     - `test_stale_run_skipped` — mock API returns COMPLETED run → process_fn never called, message ACKed
     - `test_fresh_run_processed` — mock API returns QUEUED run → process_fn called
     - `test_missing_task_id_skips_guard` — message without task_id → process_fn called (no guard)
     - `test_guard_api_failure_proceeds` — mock API raises → process_fn called anyway

2. [ ] Add `story_id` staleness guard for architect consumer dedup
   - **Input**: `services/langgraph/src/consumers/_base.py`, `shared/contracts/dto/story.py`
   - **Output**: Extend the guard from step 1: if message has `story_id` but no `task_id` (architect pattern), check story status. Skip if story is in terminal state {COMPLETED, ARCHIVED, FAILED}. This replaces the guard in `architect.py:84-99` with a centralized version. Remove the duplicate guard from `architect.py`.
   - **Test**: Unit test:
     - `test_stale_story_skipped` — story COMPLETED → skipped
     - `test_fresh_story_processed` — story CREATED → processed

3. [ ] Add periodic orphan response stream cleanup in scheduler
   - **Input**: `services/scheduler/src/main.py`, `shared/redis/client.py`
   - **Output**: New `queue_cleanup_worker()` function in `services/scheduler/src/tasks/queue_cleanup.py`. Runs every 10 minutes:
     - `SCAN` for `po:response:*` keys
     - For each: check TTL/idle time via `OBJECT IDLETIME`. If idle > 600s (10 min, well beyond the 5-min PO timeout) → `DELETE`
     - Log count of cleaned streams
     - Also `SCAN` for `worker:*:input` and `worker:*:output` patterns with same idle threshold
   - Add to scheduler's `main()` as a new `asyncio.create_task`.
   - **Test**: Unit test in `services/scheduler/tests/unit/test_queue_cleanup.py`:
     - `test_orphan_response_streams_cleaned` — mock SCAN returns 3 streams, all idle > 600s → all deleted
     - `test_fresh_response_streams_kept` — mock SCAN returns streams idle < 600s → none deleted
     - `test_worker_streams_cleaned` — mock SCAN returns orphan worker streams → deleted

4. [ ] Wire `JOB_TTL_SECONDS` to periodic XTRIM
   - **Input**: `shared/queues.py` (JOB_TTL_SECONDS, QUEUE_TOPOLOGY), `services/scheduler/src/tasks/queue_cleanup.py`
   - **Output**: In the same `queue_cleanup_worker()`, after orphan stream cleanup, run XTRIM on all task queues (from QUEUE_TOPOLOGY) using MINID strategy: compute `min_id` from current time minus `JOB_TTL_SECONDS` (7 days), trim entries older than that. This is a safety net — messages older than 7 days are definitely stale.
   - Convert JOB_TTL_SECONDS to a Redis stream ID timestamp (ms since epoch - TTL*1000).
   - **Test**: Unit test:
     - `test_xtrim_called_for_all_queues` — verify XTRIM MINID called for each queue in topology
     - `test_xtrim_minid_calculation` — verify the minid is correctly computed from current time - 7 days

5. [ ] Integration test: stale message skip in consumer loop
   - **Input**: Steps 1-2 outputs
   - **Output**: Integration test in `services/langgraph/tests/integration/test_stale_message_guard.py`:
     - Publish a DeployMessage with a task_id that corresponds to a COMPLETED run in the test DB
     - Start consumer, verify it ACKs the message without running DevOps subgraph
     - Publish a fresh DeployMessage with QUEUED run → verify it processes normally
   - **Test**: Self-testing (integration test IS the test)

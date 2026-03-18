# Убрать result_parser из wrapper, добавить watchdog-логику

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

This task is the third and final step of the "decouple worker from shared" initiative (see docs/brainstorms/decouple-worker-from-shared.md). Steps 1 (HTTP server) and 2 (remove orchestrator-cli) are already done.

Currently `execute_agent()` in wrapper.py still parses agent stdout using `ResultParser` (lines 568-584) to extract `<result>` JSON tags, `## REJECTED`, and `## BLOCKED` markers. The `process_message()` method then has a priority chain: HTTP result > stdout result > error > watchdog. With the HTTP server in place, stdout parsing is redundant — the agent reports results via `curl localhost:9090/{complete,failed,blocker}`.

**What changes**: Remove `result_parser.py`, simplify `execute_agent()` to only run the subprocess (no result parsing), and simplify `process_message()` to only check the HTTP result event (no stdout fallback).

## Steps

1. [ ] Simplify `execute_agent()` — remove stdout parsing
   - **Input**: `packages/worker-wrapper/src/worker_wrapper/wrapper.py`
   - **Output**: `execute_agent()` no longer imports or calls `ResultParser`. Returns `None` always (its only job is to run the subprocess; results come via HTTP). Still does: session management, runner selection, command build, subprocess exec, session capture from Claude JSON, git enrichment (but enrichment data goes nowhere since HTTP already has the result — remove `_enrich_result_with_git` and `_collect_and_archive` calls from execute_agent, move archive to process_message post-HTTP). Actually: git SHA and report collection should still happen, but as side effects (archive task) not as return value enrichment. Let execute_agent return None, move archive/report collection to process_message.
   - **Test**: Unit test: `execute_agent` with mocked subprocess returns None, no ResultParser import errors

2. [ ] Simplify `process_message()` — remove stdout fallback branch
   - **Input**: `packages/worker-wrapper/src/worker_wrapper/wrapper.py`
   - **Output**: After `execute_agent()` completes:
     - If `_result_event.is_set()` → log "result via HTTP", done
     - If exception from execute_agent → publish `{"status": "failed", "error": ...}`
     - Else (agent exited clean, no HTTP result) → watchdog: publish `{"status": "failed", "error": "Agent exited without reporting result"}`
     - Remove the `elif result:` branch entirely (no more stdout fallback)
     - Move `_collect_and_archive()` call here (runs regardless of result source)
   - **Test**: Unit test in test_http_integration.py: verify no stdout fallback path exists

3. [ ] Delete `result_parser.py` and its tests
   - **Input**: `packages/worker-wrapper/src/worker_wrapper/result_parser.py`, `tests/unit/test_result_parser.py`, `tests/unit/test_blocked_parsing.py`, `tests/unit/test_reject_parsing.py`
   - **Output**: Files deleted. No remaining imports of `ResultParser` or `ResultParseError` anywhere.
   - **Test**: `grep -r result_parser packages/worker-wrapper/` returns nothing

4. [ ] Update remaining tests
   - **Input**: `tests/component/test_full_cycle.py`, `tests/unit/test_http_integration.py`
   - **Output**: 
     - `test_full_cycle.py`: `execute_agent` now returns None. Update assertions — no more "raw_output" or "status" in return value. Test should verify subprocess was called, session was captured, but not check for parsed result.
     - `test_http_integration.py`: Remove `test_stdout_result_used_as_fallback` (that path no longer exists). Update `test_http_result_takes_priority_over_stdout` → becomes `test_http_result_published` (simpler, no priority logic). Keep `test_no_http_no_stdout_publishes_failed` but rename to `test_watchdog_publishes_failed_when_no_http_result`.
   - **Test**: `make test-unit` in worker-wrapper passes (all tests green)

5. [ ] Clean up unused methods on WorkerWrapper
   - **Input**: `packages/worker-wrapper/src/worker_wrapper/wrapper.py`
   - **Output**: Remove `_enrich_result_with_git()` if no longer called. `_extract_git_commit_sha()`, `_get_git_head()` can stay if archive logic uses them. Review and remove dead code.
   - **Test**: `make test-unit` passes, `make lint` passes


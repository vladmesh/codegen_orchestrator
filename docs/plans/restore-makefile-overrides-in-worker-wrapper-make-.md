# Restore Makefile overrides in worker-wrapper (make migrate broken)

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

`make migrate` inside worker containers is broken because `make dev-start svc=db` tries to run `docker compose up -d db` — workers have no Docker CLI/socket. The fix existed (commit `1ab997bb`) but was removed in `b8864abd` when the `orchestrator` CLI was deleted.

Since task-1b2bdf73 landed, the worker-wrapper HTTP server on `localhost:9090` now includes a compose proxy (`POST /infra/compose`) that forwards to worker-manager. Workers no longer need to know `WORKER_MANAGER_URL` or their own worker_id — everything goes through `localhost:9090`.

The Makefile override just needs to `curl -sf -X POST http://localhost:9090/infra/compose` with the right JSON payload.

## Steps

1. [ ] Add `_inject_makefile_overrides()` method to WorkerWrapper
   - **Input**: `packages/worker-wrapper/src/worker_wrapper/wrapper.py`
   - **Output**: New method that reads Makefile, checks for idempotency marker (`# --- orchestrator overrides ---`), appends `dev-start` and `dev-stop` override targets using `curl http://localhost:9090/infra/compose`. `dev-start` passes `$(svc)` as service arg: `{"args": ["up", "-d", "--wait", "$(svc)"], "cwd": "."}`. `dev-stop` passes `{"args": ["down", "--remove-orphans"], "cwd": "."}`. No env vars needed — localhost:9090 is always available.
   - **Test**: Unit test — create temp Makefile, call method, verify override appended with correct curl commands. Second call is no-op (idempotency).

2. [ ] Call `_inject_makefile_overrides()` in `process_message()`
   - **Input**: `packages/worker-wrapper/src/worker_wrapper/wrapper.py` — `process_message()` method
   - **Output**: Add `self._inject_makefile_overrides()` call after `self._fix_venv_shebangs()` (after line 123)
   - **Test**: Covered by step 3 tests

3. [ ] Unit tests for injection logic
   - **Input**: `packages/worker-wrapper/tests/unit/test_makefile_overrides.py` (new file)
   - **Output**: Tests covering:
     - Override injected with correct curl commands to localhost:9090
     - Idempotent — second call doesn't duplicate
     - No Makefile → no-op (no crash)
     - `$(svc)` variable passed correctly for service selection
   - **Test**: Self-contained unit tests (filesystem only, no Docker)


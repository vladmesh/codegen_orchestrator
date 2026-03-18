# HTTP-сервер в worker-wrapper (localhost:9090) — complete/failed/blocker endpoints

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

This task is the first step of the "decouple worker from shared" initiative (see docs/brainstorms/decouple-worker-from-shared.md). Currently, the agent process reports results by writing `<result>JSON</result>` to stdout, which worker-wrapper parses after the process exits. This is fragile: if parsing fails, the agent has already exited and cannot retry.

The goal is to add a localhost HTTP server (port 9090) inside worker-wrapper that runs as a parallel asyncio task alongside the agent subprocess. The agent will call `curl POST localhost:9090/complete` (or `/failed`, `/blocker`) to report results. The server validates the payload and returns 400 if invalid, giving the agent a chance to fix and retry. On success, the server publishes the result to the existing `worker:{id}:output` Redis stream.

Current state:
- `packages/worker-wrapper/src/worker_wrapper/wrapper.py` — main WorkerWrapper class, handles Redis I/O, subprocess execution, result parsing
- `packages/worker-wrapper/src/worker_wrapper/result_parser.py` — stdout parsing logic (will be replaced in a later task)
- `packages/worker-wrapper/src/worker_wrapper/main.py` — entrypoint
- `packages/worker-wrapper/src/worker_wrapper/config.py` — WorkerWrapperConfig (pydantic-settings)
- Consumer (langgraph worker_spawner) reads from `worker:{id}:output` and expects: `status`, `content`, `commit_sha`, `branch`, `error`, `reject_reason`, `block_reason` fields

## Steps

1. [ ] Pydantic request/response models for HTTP endpoints
   - **Input**: brainstorm spec (POST /complete, /failed, /blocker payloads)
   - **Output**: `packages/worker-wrapper/src/worker_wrapper/http_models.py` with `CompleteRequest`, `FailedRequest`, `BlockerRequest` models and helper to convert to Redis output format
   - **Test**: unit test — validate models accept valid payloads, reject missing fields, verify Redis output dict format matches what worker_spawner expects (status, commit_sha, branch, error, block_reason fields)

2. [ ] HTTP server module (aiohttp or bare asyncio HTTP)
   - **Input**: http_models.py, wrapper config (worker_id from env, output_stream)
   - **Output**: `packages/worker-wrapper/src/worker_wrapper/http_server.py` — async context manager or start/stop class. Binds localhost:9090. Three POST routes. Validates with Pydantic, returns 400 on validation error, 200 on success. Accepts a callback (async callable) to publish result to Redis. Uses an `asyncio.Event` to signal that a result was received (so wrapper can detect it).
   - **Test**: unit test — start server, POST valid/invalid payloads via aiohttp or urllib, verify callback called with correct dict, verify 400 on bad payload, verify asyncio.Event is set after successful POST

3. [ ] Integrate HTTP server into WorkerWrapper lifecycle
   - **Input**: wrapper.py, main.py, http_server.py
   - **Output**: Modified `wrapper.py` — start HTTP server before launching agent subprocess, stop after agent exits. The server's publish callback calls `self.redis.publish(self.config.output_stream, data)`. Add `result_received` asyncio.Event. In `execute_agent`, after subprocess completes, check if result was already received via HTTP — if yes, skip stdout parsing. If no HTTP result and no stdout result — publish `failed`. Add `http_port` to config (default 9090).
   - **Test**: unit test — mock Redis, start wrapper with HTTP server, simulate agent calling /complete, verify result published to output stream and stdout parsing skipped

4. [ ] Watchdog logic: handle agent exit without HTTP result
   - **Input**: wrapper.py (execute_agent method)
   - **Output**: After subprocess exits, if `result_received` event is NOT set — check stdout parsing as fallback (backward compat). If neither HTTP nor stdout result found — auto-publish `{"status": "failed", "error": "Agent exited without reporting result"}`. This preserves backward compatibility during the transition period while orchestrator-cli still exists.
   - **Test**: unit test — agent exits without calling HTTP endpoint AND without stdout result → verify failed status published. Agent exits with stdout result but no HTTP → verify stdout result still works (backward compat).

5. [ ] Integration test: full HTTP→Redis flow
   - **Input**: all new modules + real Redis (testcontainers or local)
   - **Output**: `packages/worker-wrapper/tests/component/test_http_server_flow.py` — start wrapper with real Redis, send HTTP POST /complete with commit+summary, verify message appears on `worker:{id}:output` stream with correct format. Test /failed and /blocker paths too.
   - **Test**: integration test (requires Redis)



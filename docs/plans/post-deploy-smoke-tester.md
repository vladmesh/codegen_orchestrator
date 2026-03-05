# Plan: Post-Deploy Smoke Tester (#25)

## Context

After deploy completes, we have no verification that the service actually works.
The deployer node reports success based on GitHub Actions `deploy.yml` exit code, but
the container might crash on startup, fail health checks, or never bind its port.

**Scope (MVP)**: Deterministic checks only, no LLM.
- Backend modules: `GET /health` → HTTP 200
- Telegram bot modules: Telethon `/start` → non-empty response
- Frontend: out of scope

**Data available at smoke time** (from `DevOpsState`):
- `allocated_resources` — dict keyed by `handle:port`, each has `server_ip`, `port`, `service_name` (module name)
- `project_spec.config.modules` — `["backend"]` or `["backend", "tg_bot"]` etc.
- `resolved_secrets` — contains `TELEGRAM_BOT_TOKEN` if tg_bot module present
- `deployed_url` — `http://{server_ip}:{port}` (first resource only)

**Integration point**: new node `smoke_tester` after `deployer` in DevOps subgraph.

## Steps

### 1. [x] Add `smoke_result` to DevOpsState

- **Input**: `services/langgraph/src/subgraphs/devops/state.py`
- **Output**: New field `smoke_result: dict | None` in `DevOpsState`
- **Test**: Unit test — verify `DevOpsState` accepts `smoke_result` key

### 2. [x] Create `smoke_tester` node (backend HTTP health check)

- **Input**: `services/langgraph/src/subgraphs/devops/nodes.py`, `allocated_resources`, `project_spec`
- **Output**: New `SmokeTesterNode(FunctionalNode)` in a new file `services/langgraph/src/subgraphs/devops/smoke.py`:
  - Iterate `allocated_resources`, match `service_name` against modules
  - For `backend`: `httpx.AsyncClient.get(f"http://{server_ip}:{port}/health", timeout=10)` with 3 retries (5s delay between)
  - Return `smoke_result: {"status": "pass"|"fail", "checks": [{"module": ..., "result": ..., "detail": ...}]}`
  - On fail: append to `errors`
- **Test**: Unit test with mocked httpx — pass case (200), fail case (500), timeout case, retry logic

### 3. [x] Add Telethon `/start` check for tg_bot modules

- **Input**: `smoke.py`, `resolved_secrets` (for `TELEGRAM_BOT_TOKEN`)
- **Output**: Extend `SmokeTesterNode`:
  - For `tg_bot` module:
    1. Get bot token from `resolved_secrets["TELEGRAM_BOT_TOKEN"]`
    2. Call Bot API `getMe` via httpx to get bot username
    3. Connect Telethon client (using orchestrator's QA session)
    4. Send `/start` to `@{bot_username}`
    5. Wait up to 15s for response
    6. Pass if any non-empty response received
  - Env vars needed: `TELETHON_API_ID`, `TELETHON_API_HASH`, `TELETHON_SESSION_PATH`
  - Graceful skip if Telethon env vars not configured (log warning, don't fail)
- **Test**: Unit test with mocked Telethon client — pass case, timeout case, missing env skip
- **Infra prerequisite**: One-time manual Telethon session authorization (document in README/ops guide)

### 4. [x] Wire smoke_tester into DevOps subgraph

- **Input**: `services/langgraph/src/subgraphs/devops/graph.py`
- **Output**:
  - `deployer` → `smoke_tester` → END (instead of `deployer` → END)
  - Conditional: if `deployer` sets errors → skip smoke, go to END
  - `smoke_tester` runs only when `deployed_url` is set
- **Test**: Unit test — graph topology: verify `smoke_tester` is in the compiled graph, verify routing skips smoke on deploy failure

### 5. [x] Handle smoke failure in deploy_worker

- **Input**: `services/langgraph/src/workers/deploy_worker.py`
- **Output**:
  - After `devops_subgraph.ainvoke()`, check `smoke_result`
  - If smoke failed: set task status to `failed`, notify user "Deployed but smoke test failed: {details}"
  - Project status stays `active` (deploy succeeded, service is just unhealthy)
  - Include smoke details in task result
- **Test**: Unit test — deploy_worker with mocked subgraph returning smoke failure

### 6. [x] Add `telethon` dependency + env vars to langgraph service

- **Input**: `services/langgraph/requirements.in`, `docker-compose.yml`
- **Output**:
  - `telethon` in requirements.in, regenerate lock file
  - Env vars in compose: `TELETHON_API_ID`, `TELETHON_API_HASH`, `TELETHON_SESSION_PATH`
  - Mount session file volume (optional, only if path configured)
- **Test**: `make build` passes, service starts without Telethon vars (graceful skip)

### 7. [x] Update `/e2e-run` skill to use smoke results

- **Input**: `.claude/skills/e2e-run/SKILL.md`
- **Output**: Update three sections:
  - **Step 4 (Monitor)**: After deploy task reaches terminal status, fetch task result and extract `smoke_result`. Distinguish "deploy failed" vs "deploy succeeded, smoke failed" in polling output.
  - **Step 5b (Verify)**: Read `smoke_result` from deploy task's `result` JSON (`curl -s http://localhost:8000/api/tasks/$DEPLOY_TASK | jq '.result.smoke_result'`). Log per-module check results. Keep the manual `curl /health` as independent cross-check.
  - **Step 7 (Report)**: Add `> **Smoke**: pass (backend: 200) / fail (tg_bot: timeout)` to report header. Include smoke details in Timeline.
- **Test**: Manual — run a test E2E after all other steps are done, verify smoke results appear in report

## Deviations

- **Step 6**: Plan referenced `requirements.in` but project uses `pyproject.toml` for dependencies. Added telethon to `pyproject.toml` and ran `make lock-deps`.
- **Step 5**: `deploy_worker.py` hit PLR0915 (too many statements) after adding smoke handling. Extracted 3 helper functions: `_handle_smoke_failure`, `_handle_deploy_success`, `_build_subgraph_input`.
- **Step 2**: Added `HTTP_OK = 200` constant to avoid PLR2004 magic number lint error.
- **Step 3**: Used `/var/lib/telethon/` instead of `/tmp/` for session path default to avoid S108 (insecure temp directory) lint warning.
- **General**: No deviations from the overall architecture or scope. All 7 steps implemented as planned.

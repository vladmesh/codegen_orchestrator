# E2E Report: todo_api — Deploy race condition: scaffold CI satisfied gate, implementation image never deployed

> **Date**: 2026-03-03
> **Project**: todo_api (project_id: `f6a3c185-594d-489b-b67b-064afb953dc0`)
> **Task**: eng-5828a1e64533
> **Test level**: C
> **Status**: Failed
> **Worker audit**: [todo_api-20260303-levelC-2-worker.md](./todo_api-20260303-levelC-2-worker.md)

---

## Timeline

| Time (UTC) | Event |
|------------|-------|
| 00:03:22 | Project created, engineering task published to queue |
| 00:03:28 | Worker container `worker-dev-todo-api-d1b2f152` created |
| 00:03:57 | Scaffold phase started (copier template: `gh:vladmesh/service-template`) |
| 00:04:14 | Scaffold phase completed and verified (17s) |
| 00:04:15 | Claude agent started implementation |
| 00:04:18 | CI Run #22601627091 (scaffold commit `b476c16d`): started |
| ~00:06:00 | CI Run #22601627091 (scaffold): **passed** |
| 00:11:14 | Implementation commit `50e4c995`: `feat: implement TODO CRUD API with PostgreSQL` |
| 00:11:35 | Engineering worker starts CI gate check |
| 00:11:36 | **BUG**: CI gate finds scaffold CI run #22601627091 (already passed) — accepts it as the implementation CI |
| 00:11:36 | CI Run #22601838862 (implementation `50e4c995`): started (but not seen by gate) |
| 00:11:36 | Worker deleted, deploy task auto-triggered |
| 00:11:37 | DevOps subgraph starts: env analysis, secret resolution |
| 00:11:39 | Secrets resolved (13 vars, `BACKEND_PORT=8000` — correct) |
| 00:11:49 | Deploy workflow #22601845043 dispatched on GitHub |
| 00:11:54 | Deploy job starts: SSH to server, pulls image from registry |
| 00:12:58 | Deploy completes — pulls **scaffold image** (CI run 1), not implementation image |
| 00:13:09 | Service-deployment record created (status: running) |
| 00:13:39 | CI Run #22601838862 (implementation): **passed** — but deploy already finished |

**Engineering duration**: ~8 min (scaffold 17s, implementation ~7 min)
**Deploy duration**: ~1.5 min (succeeded but deployed wrong image)

## Engineering Results

- 3 commits (initial, scaffold, implementation), 2 CI runs — all passed first try
- No CI fix cycles needed
- Worker produced AUDIT_REPORT.md with useful findings

## Verification Results

**What works:**
- `/health` → `{"status":"ok"}`
- Containers running: `backend` (0 restarts), `db` (healthy, 0 restarts)
- `.env` correct: `BACKEND_PORT=8000` (previous bug fixed)

**What fails:**
- `GET /todos` → 404 Not Found
- `POST /todos` → 404 Not Found
- OpenAPI shows only: `/health`, `/users`, `/users/{user_id}` — no todo endpoints
- Docker image is from scaffold CI (file timestamps 00:05, `routers/` only has `users.py`)

## Problems Found

### Problem 1: CI gate checks wrong CI run — scaffold CI satisfies gate for implementation commit (DEPLOY BLOCKER)

- **Type**: orchestrator
- **Severity**: critical
- **Status**: **FIXED** (commit `9ac91d6` → next commit)
- **Description**: After the developer worker pushes the implementation commit and finishes, the engineering-worker's CI gate (`_wait_for_ci_and_fix`) immediately checks for a passing CI run. It calls `get_latest_workflow_run()` with `created_after=developer_started_at` (set before the engineering subgraph started, ~00:03:28). The GitHub API returns the **scaffold CI** (run #22601627091, completed at ~00:06) because it's the latest completed run after `created_after`. The implementation CI (run #22601838862, started at 00:11:36) hasn't been created yet at the moment of the check, or was just created and is still queued.
- **Root cause**: `_wait_for_ci_and_fix` uses a time-based filter (`created_after`) to find the relevant CI run, but `developer_started_at` is set before the engineering subgraph starts (line 767), not after the implementation commit is pushed. This means the scaffold CI (created during the subgraph execution) is within the filter window. With `per_page=1`, the API returns the most recent run, which is the already-completed scaffold CI. The function sees it passed and returns immediately without waiting for the implementation CI.
- **Impact**: Deploy is triggered with the scaffold Docker image (no implementation code). The deployed API has only framework-generated user routes, no todo routes.
- **Fix applied**: Added `head_sha` parameter to `get_latest_workflow_run` and `wait_for_workflow_completion` in `shared/clients/github.py`. On the initial CI check (attempt 0), the gate now filters by `commit_sha` from the engineering result, ensuring only the CI run for the actual implementation commit is considered. On CI fix retries (attempt 1+), `head_sha` is cleared (the fix developer pushes a new commit with a different SHA) and falls back to `created_after` filtering, which works correctly for retries since the timestamp is captured after the failed run is observed.
  - `shared/clients/github.py` — `get_latest_workflow_run` / `wait_for_workflow_completion`: added `head_sha` param
  - `services/langgraph/src/workers/engineering_worker.py` — `_wait_for_ci_and_fix`: added `commit_sha` param, clear on retry
  - `services/langgraph/src/workers/engineering_worker.py` — `_handle_engineering_success`: passes `result.get("commit_sha")` to CI gate

### Problem 2: `generated/` directories empty in repo — framework code generation incomplete for Todo domain

- **Type**: template
- **Severity**: minor (masked by Problem 1 — this would cause a runtime error but didn't reach production)
- **Description**: The `services/backend/src/generated/protocols.py` in the Docker image only contains `UsersControllerProtocol` (no `TodosControllerProtocol`). The `shared/generated/schemas.py` only contains User schemas (no `TodoCreate`, `TodoRead`, `TodoUpdate`). The todo router imports these missing types, so even with the correct Docker image, the todo routes would fail to load.
- **Root cause**: Either `make generate-from-spec` wasn't run after updating the spec files, or the spec wasn't updated to include the Todo model before generation. The worker's audit report confirms the spec-first workflow worked during development, but the generated files may not have been committed or the Dockerfile builds from pre-generation state.
- **Suggested fix**: Ensure the CI workflow runs `make generate-from-spec` as part of the Docker build, or verify that generated files are committed to the repo.

### Problem 3: BACKEND_PORT fix confirmed — previous critical bug resolved

- **Type**: orchestrator
- **Severity**: (resolved)
- **Description**: The previous run's critical bug (`BACKEND_PORT=QkSBLev68L23BgZz...` random token) has been fixed. `BACKEND_PORT` is now classified as "computed" in the env analyzer and correctly resolved to `8000` from the port allocation. The `.env` on the server confirms `BACKEND_PORT=8000`.

## Positive Observations

- First-try CI pass — no fix cycles needed
- Fast implementation: ~7 min from agent start to code push
- BACKEND_PORT resolution bug from previous run is fixed
- Worker produced detailed audit report with actionable findings
- Scaffold completed in 17s
- All 13 env variables correctly resolved by DevOps subgraph
- Server containers healthy with 0 restarts (scaffold code runs fine)

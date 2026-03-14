# Unify workspace management: repo_id-based addressing, remove legacy workspace creation

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Workspace browser in admin panel is broken for all scaffolded projects. Root cause: two parallel workspace systems exist — scaffolder creates workspaces at `/data/workspaces/{repo_id}/`, but introspection API and frontend look at `/data/workspaces/{project_id}/` and `/tmp/codegen/workspaces/{project_id}/workspace/`. These are different IDs.

The fix: unify around `repo_id` as the sole workspace key. Remove legacy workspace creation from worker-manager — scaffolder is the single source of truth.

### Current state
- **Scaffolder**: stores at `/data/workspaces/{repo_id}/` ✓
- **Worker-manager spawn** (`manager.py:617-642`): 3 branches — `repo_id` (correct), `project_id` (legacy), `worker_id` (legacy)
- **Introspection API** (`workspaces.py`, `introspect.py`): looks for `{project_id}` in both paths — always misses scaffolded workspaces
- **Frontend**: passes `project_id` to workspace browser URLs — matches nothing
- **Redis meta**: `repo_id` is NOT stored in `worker:meta:{id}`, only `project_id` is
- **GC** (`manager.py:299-316, 320-370`): scans both `WORKSPACE_BASE_PATH` and `SCAFFOLDED_WORKSPACE_PATH`
- **ComposeRunner**: uses `WORKSPACE_BASE_PATH` as fallback for `workspace_dir`
- **Worker detail model**: `WorkerSummary` has no `repo_id` field
- **Frontend**: no `Repository` type, no API call to fetch repos

## Steps

1. [ ] Store `repo_id` in Redis worker metadata
   - **Input**: `services/worker-manager/src/manager.py` (line 696-699)
   - **Output**: `repo_id` persisted alongside `project_id` in `worker:meta:{worker_id}` hash
   - **Test**: unit test — when `repo_id` provided, verify `redis.hset` includes `repo_id`

2. [ ] Remove legacy workspace creation from manager
   - **Input**: `services/worker-manager/src/manager.py` (lines 617-642), `workspace.py`
   - **Output**: Only `repo_id` branch remains. If `repo_id` is None → `RuntimeError`. Remove `create_workspace()`, `get_or_create_project_workspace()`, `get_workspace_host_path()` from `workspace.py`. Keep `get_scaffolded_workspace()` and `remove_workspace()` (used by GC).
   - **Test**: unit test — calling without `repo_id` raises RuntimeError; calling with `repo_id` uses `get_scaffolded_workspace`

3. [ ] Simplify GC and delete_worker to use SCAFFOLDED_WORKSPACE_PATH only
   - **Input**: `services/worker-manager/src/manager.py` (lines 154, 192-193, 299-316, 320-370, 584)
   - **Output**: `garbage_collect_orphans` scans only `SCAFFOLDED_WORKSPACE_PATH`. `delete_worker` doesn't remove workspace dirs (scaffolded workspaces are persistent, cleaned only by time-based GC). `failure_count >= 2` no longer removes workspace (no legacy path to clean). ComposeRunner gets `SCAFFOLDED_WORKSPACE_PATH`.
   - **Test**: update existing GC tests to reflect single scan path

4. [ ] Remove `WORKSPACE_BASE_PATH` from config and docker-compose
   - **Input**: `services/worker-manager/src/config.py`, `services/worker-manager/src/main.py`, `docker-compose.yml`
   - **Output**: `WORKSPACE_BASE_PATH` setting removed. `app.state.workspace_base_path` removed. Docker-compose worker-manager volume `/tmp/codegen/workspaces` removed. ComposeRunner initialized with `SCAFFOLDED_WORKSPACE_PATH`.
   - **Test**: config tests still pass, app starts without `WORKSPACE_BASE_PATH`

5. [ ] Fix workspace introspection API — address by `repo_id`
   - **Input**: `services/worker-manager/src/routers/workspaces.py`, `introspect.py`
   - **Output**: `workspaces.py` endpoints use `repo_id` param, resolve to `SCAFFOLDED_WORKSPACE_PATH/{repo_id}/`. `introspect.py` `_get_workspace_path` reads `repo_id` from Redis meta, resolves to `SCAFFOLDED_WORKSPACE_PATH/{repo_id}/`. Remove `workspace_base_path` from `app.state` references.
   - **Test**: unit tests — workspace tree returns files when repo_id dir exists; 404 when not

6. [ ] Add `repo_id` to WorkerSummary model
   - **Input**: `services/worker-manager/src/routers/introspect.py` (WorkerSummary, list_workers, get_worker_detail)
   - **Output**: `repo_id: str | None` field in WorkerSummary, populated from `worker:meta:{id}` hash
   - **Test**: unit test — worker detail response includes `repo_id`

7. [ ] Frontend: fetch repos for project, use `repo_id` in workspace browser
   - **Input**: `services/admin-frontend/src/pages/ProjectDetailPage.tsx`, `WorkerDetailPage.tsx`, `types/api.ts`, `lib/api.ts`
   - **Output**: Add `Repository` type. `ProjectDetailPage` fetches `/api/repositories/?project_id=X`, gets first repo's `id`, passes to WorkspaceBrowser as `/wm-api/workspaces/{repo_id}/...`. `WorkerDetailPage` uses `worker.repo_id` (from step 6) for workspace URLs. Fallback to worker-level `/wm-api/workers/{id}/tree` if no repo_id.
   - **Test**: manual — workspace browser shows files for lesswrong-random-bot

8. [ ] Update all existing tests
   - **Input**: `tests/unit/test_workspace.py`, `test_repo_id_workspace.py`, `test_project_id_passthrough.py`, `test_scaffold_phase.py`, `test_manager_logic.py`, `test_introspect_api.py`, `test_workspaces_api.py`, `tests/e2e/test_dev_env_smoke.py`
   - **Output**: Remove tests for `create_workspace`, `get_or_create_project_workspace`, `get_workspace_host_path`. Update mocks to use `get_scaffolded_workspace` only. Update tests that expect `WORKSPACE_BASE_PATH` behavior. E2E smoke test uses `SCAFFOLDED_WORKSPACE_PATH`.
   - **Test**: `make test-worker-manager-unit` passes


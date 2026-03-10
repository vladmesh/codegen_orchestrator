# Worker-manager mounts workspace volume by repo_id

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

The scaffolder service (already implemented) runs *before* the architect and creates project workspaces at `/data/workspaces/{repo_id}/` on the host. Currently, the worker-manager *also* does scaffolding inside the worker container (`_run_scaffold_phase`) and manages its own workspaces at `/tmp/codegen/workspaces/{project_id}/workspace/`. This task removes the duplicate scaffold logic from worker-manager and makes it mount the pre-built scaffolder workspace instead.

**Key insight**: Two workspace paths exist — scaffolder writes to `/data/workspaces/{repo_id}/`, worker-manager creates at `/tmp/codegen/workspaces/`. After this change, worker-manager reads the scaffolder's workspace by `repo_id` from a shared host volume, and the old `/tmp/codegen/workspaces/` path is only used for non-project (ad-hoc) workers.

**Blocker resolved**: task-2378004c (Architect receives tree + specs) is done.

**Files to change**:
- `services/worker-manager/src/manager.py` — remove `_run_scaffold_phase`, change workspace resolution
- `services/worker-manager/src/workspace.py` — add `get_scaffolded_workspace()` helper
- `shared/contracts/queues/worker.py` — add `repo_id` to WorkerConfig, deprecate ScaffoldConfig usage
- `services/langgraph/src/nodes/developer.py` — pass `repo_id` instead of ScaffoldConfig, stop setting scaffold status
- `services/langgraph/src/clients/worker_spawner.py` — pass `repo_id` through to WorkerConfig
- `services/langgraph/src/consumers/engineering.py` — fetch repo_id, pass to subgraph state
- `services/langgraph/src/subgraphs/engineering.py` — add repo_id to EngineeringState
- `docker-compose.yml` — mount shared workspace volume into worker-manager
- Tests in both services

## Steps

1. [ ] Add `repo_id` to WorkerConfig and EngineeringState contracts
   - **Input**: `shared/contracts/queues/worker.py`, `services/langgraph/src/subgraphs/engineering.py`
   - **Output**: `WorkerConfig` gains `repo_id: str | None = None` field. `EngineeringState` TypedDict gains `repo_id: str | None`. `ScaffoldConfig` class and `scaffold_config` field on WorkerConfig remain (not deleted yet — removed in step 4 after dependents are updated).
   - **Test**: Unit test: `WorkerConfig(repo_id="repo-abc123", ...)` serializes correctly. Backward compat: existing configs without `repo_id` still validate.
   - ⚠️ needs-approval (shared/contracts change)

2. [ ] Add scaffolded workspace resolver to worker-manager
   - **Input**: `services/worker-manager/src/workspace.py`, `docker-compose.yml`
   - **Output**: New function `get_scaffolded_workspace(scaffolded_base_path: str, repo_id: str) -> tuple[Path, bool]` that returns `(path, exists)` for `/data/workspaces/{repo_id}/`. `docker-compose.yml` adds `${WORKSPACE_HOST_PATH:-/data/workspaces}:/data/workspaces` volume mount to worker-manager service. New setting `SCAFFOLDED_WORKSPACE_PATH` in worker-manager config.
   - **Test**: Unit test: `get_scaffolded_workspace("/data/workspaces", "repo-123")` returns correct path and exists=True when dir exists, exists=False when missing.

3. [ ] Worker-manager mounts scaffolded workspace by repo_id
   - **Input**: `services/worker-manager/src/manager.py`
   - **Output**: In `create_worker_with_capabilities()`, when `config.repo_id` is set: resolve workspace via `get_scaffolded_workspace()`, mount it into container at `/workspace`. Skip git clone (repo already set up by scaffolder). Skip `_run_scaffold_phase` entirely. If scaffolded workspace doesn't exist → raise error (scaffolder should have run first). For workers without `repo_id` (ad-hoc), keep existing behavior unchanged.
   - **Test**: Unit test: mock Docker + workspace filesystem. Verify: (a) repo_id present → mounts `/data/workspaces/{repo_id}` as `/workspace`, no scaffold or clone. (b) repo_id present but dir missing → raises RuntimeError. (c) no repo_id → falls through to old project_id workspace logic.

4. [ ] Remove `_run_scaffold_phase` and ScaffoldConfig from worker-manager
   - **Input**: `services/worker-manager/src/manager.py`, `shared/contracts/queues/worker.py`
   - **Output**: Delete `_run_scaffold_phase()` method. Remove `scaffold_config` parameter from `create_worker_with_capabilities()`. Remove scaffold verification block (copier markers check). Remove `ScaffoldConfig` class from contracts. Remove `scaffold_config` field from `WorkerConfig`. Update consumer.py to stop passing scaffold_config.
   - **Test**: Unit test: verify `create_worker_with_capabilities` no longer accepts `scaffold_config`. Verify `_run_scaffold_phase` method doesn't exist on `WorkerManager`.
   - ⚠️ needs-approval (shared/contracts change — removing ScaffoldConfig)

5. [ ] Developer node passes repo_id instead of ScaffoldConfig
   - **Input**: `services/langgraph/src/nodes/developer.py`, `services/langgraph/src/clients/worker_spawner.py`
   - **Output**: Remove `_build_scaffold_config()` method from DeveloperNode. `request_spawn()` accepts `repo_id` instead of `scaffold_config`, passes it into WorkerConfig. Developer node fetches primary repository via `api_client.get_primary_repository(project_id)` and extracts `repo_id`. Developer node no longer updates project status to "scaffolded"/"scaffold_failed" (scaffolder handles this). The draft-project "scaffolding" guard in `run()` is updated to check for `scaffolded` status instead (scaffolder must have run).
   - **Test**: Unit test: mock API returns project with primary repo → verify `request_spawn` called with `repo_id` and no `scaffold_config`. Test: action="create" + status not "scaffolded" → returns blocked error.

6. [ ] Engineering consumer passes repo_id through to subgraph
   - **Input**: `services/langgraph/src/consumers/engineering.py`, `services/langgraph/src/subgraphs/engineering.py`
   - **Output**: Consumer fetches primary repository for project (already has `project_id`), extracts `repo_id`, adds to `subgraph_input["repo_id"]`. Engineering state carries `repo_id` to developer node. Developer node reads `repo_id` from state.
   - **Test**: Unit test: mock `api_client.get_primary_repository()` → verify `repo_id` present in subgraph_input. Integration test: full consumer flow passes repo_id through to developer node.

7. [ ] Developer node writes TASK.md to workspace before Claude invocation
   - **Input**: `services/langgraph/src/nodes/developer.py`, `services/worker-manager/src/manager.py`
   - **Output**: After worker container is created and workspace mounted, developer node's task content is injected as TASK.md into the workspace (already happens via `task_content` param → `_inject_task_md`). Verify this path works correctly with scaffolded workspace (TASK.md sits alongside the project files). Add previous task events as context section in TASK.md (fetch via API `tasks/{task_id}/events`).
   - **Test**: Unit test: verify TASK.md content includes task description and event history when events exist. Test: no events → TASK.md has description only.

8. [ ] Integration test: worker starts with pre-scaffolded workspace
   - **Input**: All changed files from steps 1-7
   - **Output**: Integration test that: (a) creates a mock scaffolded workspace at `/data/workspaces/{repo_id}/` with expected markers (.copier-answers.yml, .git), (b) calls `create_worker_with_capabilities(repo_id="test-repo-123")`, (c) verifies container mounts the correct path, (d) verifies no scaffold phase runs, (e) verifies instructions and TASK.md are injected correctly. Runs with `make test-worker-manager-integration`.
   - **Test**: This IS the integration test.

9. [ ] Update existing tests and cleanup
   - **Input**: All test files in `services/worker-manager/tests/`, `services/langgraph/tests/`
   - **Output**: Remove/update tests that reference ScaffoldConfig, `_run_scaffold_phase`, or `_build_scaffold_config`. Update mocks in developer node tests to use `repo_id` instead of `scaffold_config`. Ensure `make test-unit` passes clean.
   - **Test**: `make test-unit` green, no warnings about deprecated scaffold references.


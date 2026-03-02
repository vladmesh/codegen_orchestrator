# E2E Report: todo_api — Level C full flow (engineering + CI + deploy), run 2

> **Date**: 2026-03-02
> **Project**: todo_api (project_id: `d6e11772-7ec2-4fa8-8a75-fc5b57b77b1f`)
> **Task**: eng-a63b8038d6f4 (engineering), deploy-a63b8038d6f4 (deploy)
> **Test level**: C
> **Status**: Partial pass (engineering + CI OK, deploy failed — backend crash loop)
> **Worker audit**: [todo_api-20260302-levelC-2-worker.md](./todo_api-20260302-levelC-2-worker.md)

---

## Timeline

| Time (UTC) | Event |
|------------|-------|
| 09:52:16 | Project created via API, first attempt failed (leftover `todo-api` repo from previous run) |
| 09:52:52 | Leftover repo deleted, project re-created |
| 09:53:13 | Engineering task `eng-a63b8038d6f4` published to queue |
| 09:53:18 | Resources allocated (port 8000 on vps-267180) |
| 09:53:19 | Worker `dev-todo-api-d7b53eed` created, scaffold started |
| 09:53:36 | Scaffold commit `068d5f36` — `feat: scaffold todo-api with modules: backend` |
| 09:53:37 | Scaffold verified (copier-answers + github-workflows present) |
| 09:53:38 | Claude Code agent starts working |
| 09:53:41 | CI run #22570592559 (scaffold) → **SUCCESS** |
| 09:58:52 | Implementation commit `3599f6da` — `feat: implement TODO CRUD API with GET/POST/PATCH/DELETE /todos` |
| 09:58:58 | CI run #22570789174 (implementation) → **SUCCESS** |
| ~10:00 | Engineering task completed |
| 10:01:20 | Deploy task `deploy-a63b8038d6f4` created |
| 10:01:32 | Deploy secrets configured (9 secrets) |
| 10:01:34 | `deploy.yml` workflow dispatched |
| 10:02:38 | Docker images pulled on server, containers created |
| 10:02:56 | DB healthy, backend started |
| 10:03:11 | Backend crash loop detected (3 restarts) — deploy **FAILED** |
| 10:03:25 | Deploy task marked failed |

**Total duration**: ~11 minutes (scaffold ~18s, code gen ~5 min, CI ~1 min, deploy ~2 min)

## Verification

### Code generation: PASS

3 commits on main. Clean implementation of TODO CRUD API:
- Spec-first workflow (models.yaml → generate-from-spec → implement)
- All 4 endpoints: GET/POST/PATCH/DELETE /todos
- Unit tests included
- No CI fix cycles needed — implementation passed CI on first push

### CI: PASS

Both CI runs (scaffold + implementation) passed on first attempt. No fix cycles.

| Run | Trigger | Conclusion |
|-----|---------|------------|
| #22570592559 | push (scaffold) | success |
| #22570789174 | push (implementation) | success |

### Deploy: FAIL

Deploy workflow failed at "Deploy via SSH" step. Same failure pattern as previous Level C run:
- Docker images pulled and containers created on server
- DB container becomes healthy
- Backend container enters crash loop (3 restarts in health check window)

Deploy run: https://github.com/project-factory-organization/todo-api/actions/runs/22570889183

## Worker Audit Summary

The worker produced an excellent audit report (see linked file). Key findings:

**What worked well**: Spec-first codegen, project structure, linter tooling, test infrastructure, controller stub generation.

**Issues found by worker**:
1. Generated `TodoUpdate` schema defaults optional fields to `""` / `False` instead of `None`
2. Generated protocols have inconsistent indentation
3. Routers not auto-generated (could be, given spec info)
4. `__init__.py` files not updated by codegen
5. `ORMBase` always includes `updated_at` even when spec doesn't define it
6. Trailing whitespace in scaffolded `lifespan.py`

## Problems Found

### Problem 1: Backend crash loop on deploy — `ModuleNotFoundError: No module named 'shared.generated'`

- **Type**: template
- **Severity**: critical
- **Description**: Backend container enters crash loop on server deployment. Reproduced by SSHing into the server (`80.209.235.229`) and inspecting container logs. The crash happens during Alembic migration in `start.sh`:

  ```
  File "/app/services/backend/src/app/schemas/__init__.py", line 3, in <module>
      from shared.generated.schemas import (
  ModuleNotFoundError: No module named 'shared.generated'
  ```

  Full import chain: `start.sh` → `alembic upgrade` → `env.py` → `models` → `app/__init__.py` → `router` → `routers/todos.py` → `controllers/todos.py` → `repositories/todo.py` → `schemas/__init__.py` → `from shared.generated.schemas import ...` → **crash**.

- **Root cause**: Two bugs compound in `service-template`:

  1. **`.gitignore` excludes generated code.** `template/.gitignore` line 19 has `**/generated/`. The scaffold phase runs `make setup` → `make generate-from-spec`, which creates `shared/shared/generated/schemas.py`, but the subsequent `git add .` skips these files. Generated code is never pushed to GitHub.

  2. **CI `build-and-push` job doesn't regenerate.** In `ci.yml.jinja`, the `lint-and-test` job runs `make generate-from-spec` (line 42), but this is a separate job — its workspace doesn't carry over. The `build-and-push` job only does `actions/checkout@v4` + Docker build from a fresh checkout that has no generated files. `Dockerfile.jinja` lines 26 and 33 (`COPY shared/shared` / `COPY shared`) copy whatever is in the build context — which doesn't include `shared/shared/generated/`.

  **Why CI tests pass but deploy fails:** `lint-and-test` regenerates files in its workspace and runs tests there (not in Docker). Tests pass. Then `build-and-push` builds a Docker image from a fresh checkout → image ships without `shared/shared/generated/` → backend crashes on startup.

- **Files involved**:
  - `service-template/template/.gitignore:19` — `**/generated/` rule
  - `service-template/template/.github/workflows/ci.yml.jinja:42` — generation only in `lint-and-test` job
  - `service-template/template/.github/workflows/ci.yml.jinja:94-95` — `build-and-push` does checkout without generation
  - `service-template/template/services/backend/Dockerfile.jinja:26,33` — COPY assumes generated files exist
  - `codegen_orchestrator/services/worker-manager/src/manager.py:794` — scaffold `git add .` respects `.gitignore`

- **Suggested fixes** (in `service-template`):

  | Option | Change | Tradeoff |
  |--------|--------|----------|
  | A. Generate in `build-and-push` | Add `make generate-from-spec` step before Docker build in `ci.yml.jinja` | Needs `datamodel-code-generator` in CI runner |
  | B. Track generated files in git | Remove or negate `**/generated/` in `.gitignore` | Generated code in git, but always available |
  | C. Generate in Dockerfile | Add `RUN make generate-from-spec` to Dockerfile | Adds dev dep to prod image, slower builds |

### Problem 2: Leftover repo from previous run blocking start

- **Type**: meta
- **Severity**: minor
- **Description**: The previous Level C test run left the `todo-api` repo on GitHub, causing the new run to fail with `422 Unprocessable Entity` ("repository already exists"). Had to manually delete it.
- **Root cause**: Previous cleanup either didn't run or failed silently.
- **Suggested fix**: The e2e-run skill cleanup step should be more robust, or the create step should check for existing repos and clean them up before proceeding.

## Summary

| Phase | Status | Duration | Notes |
|-------|--------|----------|-------|
| Scaffold | PASS | ~18s | Clean, verified |
| Code generation | PASS | ~5 min | Single commit, first-try CI pass |
| CI | PASS | ~1 min | Both runs passed |
| Deploy | FAIL | ~2 min | Backend crash loop (same as previous run) |

Engineering pipeline is solid — fast scaffold, clean implementation, no CI fix cycles needed. The deploy failure is a recurring issue that needs investigation on the server side.

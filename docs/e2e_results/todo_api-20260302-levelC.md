# E2E Report: todo_api — Level C full flow: engineering + CI + deploy

> **Date**: 2026-03-02
> **Project**: todo_api (project_id: `fdecc61b-0206-4bcd-b4bd-27050e03b34e`)
> **Task**: eng-da22c2c2b5b6 (engineering), deploy-da22c2c2b5b6 (deploy)
> **Test level**: C
> **Status**: Partial pass (engineering + CI + deploy workflow OK, service crashed on start)
> **Worker audit**: [todo_api-20260302-worker.md](./todo_api-20260302-worker.md)

---

## Timeline

| Time (UTC)  | Event |
|-------------|-------|
| 00:10:23 | Task created via API |
| 00:10:31 | Engineering message published to queue |
| 00:10:37 | Worker container `worker-dev-todo-api-ae56d9dc` created |
| 00:10:37 | Scaffold phase starts (modules: backend) |
| 00:10:55 | Scaffold complete, verified (copier-answers + github-workflows present) |
| 00:10:56 | Claude Code starts in worker |
| 00:11:00 | CI run #22556190337 (scaffold commit) → **FAILED** (integration tests: missing POSTGRES_USER) |
| 00:20:14 | **Commit 1** `f275d1f` — `feat: implement TODO CRUD API with GET/POST/PATCH/DELETE /todos` |
| 00:20:22 | CI run #22556388134 → **FAILED** (integration tests: missing POSTGRES_USER env var) |
| 00:20:58 | CI gate sends fix task to worker (attempt 1) |
| 00:25:35 | **Commit 2** `0c7c9f8` — `fix: resolve Docker Compose env var interpolation for integration tests` |
| 00:25:41 | CI run #22556495490 → **SUCCESS** |
| 00:28:05 | Deploy task created (auto-triggered by CI success + skip_deploy=false) |
| 00:28:19 | CI run #22556546534 (deploy.yml) starts |
| 00:30:10 | Deploy workflow completed successfully, deployment record created |
| 00:30:10 | Deploy task marked completed, deployed_url: `http://176.223.131.124:8000` |

**Total duration**: ~20 minutes (code gen ~10 min, CI fix ~5 min, deploy ~2 min)

## Verification

### Code generation: PASS
4 commits on main. Complete Todo CRUD API with unit tests, all generated via spec-first workflow.

### CI: PASS (after 1 fix)
- First failure: `POSTGRES_USER is missing a value` in `compose.tests.integration.yml` — the `x-backend-env` anchor references `${POSTGRES_USER:?}` which fails when `.env` is not loaded.
- Fix: agent added proper `--env-file .env` to docker compose commands.
- Second CI run passed: lint, unit tests, integration tests all green.

### Deploy workflow: PASS
- deploy.yml ran: SCP compose files → SSH deploy → success.
- Deployment record created in API.

### Service reachable: FAIL
- `curl http://176.223.131.124:8000/health` → connection refused.
- **Root cause confirmed via SSH**: backend container in `Restarting` state. Crash log:
  ```
  File "/app/services/backend/src/controllers/debug.py", line 15, in <module>
      from shared.generated.events import publish_command_received
  ModuleNotFoundError: No module named 'shared.generated'
  ```
- The agent created a `debug.py` controller that imports `shared.generated.events` — a module that doesn't exist because `make generate-from-spec` was not run for the `debug` domain or the events system wasn't scaffolded.
- DB container (`infra-db-1`) was healthy; only the backend crashed.

---

## Problems Found

### Problem 1: Integration tests fail due to missing env vars from compose anchor

- **Type**: template
- **Severity**: major
- **Description**: `compose.base.yml` uses `x-backend-env` anchor with `${POSTGRES_USER:?}` (required). The integration compose inherits this via `extends`, but CI runs docker compose from repo root while env file is at repo root. The compose file's `env_file` directive loads `.env` but the anchor `x-backend-env` is evaluated before `env_file` is processed, so required vars fail.
- **Root cause**: Docker Compose evaluates `${VAR:?}` in the compose file from the shell environment, not from `env_file`. The `x-backend-env` anchor uses `:?` (required) syntax which fails if vars aren't in the shell environment, even though `env_file: ../.env` is specified.
- **Fix applied by agent**: Added `--env-file .env` to the docker compose command in Makefile, ensuring vars are available during compose file parsing.
- **Note**: This is the same class of issue as the previous run's env var problem. The template's `compose.base.yml` uses strict `${VAR:?}` syntax that's incompatible with relying solely on `env_file` directives.

### Problem 2: Backend crashes on deploy — `shared.generated` missing

- **Type**: template / agent code quality
- **Severity**: critical
- **Description**: Deploy workflow completed successfully (all steps green), but the backend container crashes on startup in a restart loop.
- **Root cause**: The agent created `services/backend/src/controllers/debug.py` which imports `from shared.generated.events import publish_command_received`. The `shared.generated` package doesn't exist — the scaffold generates `shared/shared/generated/schemas.py` but no `events` module. The agent hallucinated this import. CI passed because unit tests don't trigger this import path (they test `todos` endpoints, not `debug`), and integration tests apparently didn't hit the debug endpoint either.
- **Impact**: Service can't start at all — every container restart hits the same import error.
- **Suggested fix**: Two things: (1) The deploy workflow's healthcheck step should catch this and mark the deploy as failed. (2) The template should not include a `debug` controller with external imports, or the scaffold should generate `shared.generated.events` as a stub.

### Problem 3: Scaffold CI run also fails integration tests

- **Type**: template
- **Severity**: minor
- **Description**: The scaffold commit triggers CI which runs integration tests on the bare scaffold (before business logic). This always fails because the scaffold has no real endpoints implemented yet. This is wasted CI time (~3 min).
- **Suggested fix**: Either skip integration tests on scaffold commits (detect via commit message pattern) or make the scaffold's integration tests pass by default (test only the health endpoint which the scaffold provides).

---

## Worker Audit Highlights

The worker's audit report is thorough. Key findings:
1. **Routers not auto-generated** — most significant manual step, ~80 lines of boilerplate per domain
2. **Spec-first workflow works well** — generate-from-spec produced correct schemas, protocols, controller stubs
3. **Test infrastructure solid** — SQLite-based unit tests with transactional fixtures, fast (~1s for 19 tests)
4. **Validation pipeline works** — spec validation, lint, controller sync all catch real issues

## Summary

**Level C flow works end-to-end**: scaffold → code gen → CI pass → deploy workflow → deployment record. The orchestrator pipeline successfully delivered from "project description" to "deployed service" in ~20 minutes.

The critical gap is the final mile: the backend container crashes on startup due to a hallucinated import (`shared.generated.events`). The deploy workflow reported success because the GitHub Actions steps completed (SCP + SSH + docker compose up), but the container entered a restart loop immediately after. The deploy healthcheck didn't catch this.

Engineering quality is mostly high — the agent produced a complete CRUD API with proper spec-first workflow, unit tests, and fixed CI autonomously. However, the `debug.py` controller with a non-existent import is a code quality gap that CI didn't catch because tests didn't exercise that path.

**Cleanup**: Server cleaned via SSH — containers stopped, volumes removed, `/opt/services/todo_api` deleted.

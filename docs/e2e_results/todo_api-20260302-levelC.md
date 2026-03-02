# E2E Report: todo_api — Level C full flow (engineering + CI + deploy)

> **Date**: 2026-03-02
> **Project**: todo_api (project_id: `94e9a090-8f28-4f9a-a0f9-c1e933778cd6`)
> **Task**: eng-1c6651df5457 (engineering), deploy-1c6651df5457 (deploy)
> **Test level**: C
> **Status**: Partial pass (engineering + CI OK, deploy failed — backend crash loop)
> **Worker audit**: [todo_api-20260302-levelC-worker.md](./todo_api-20260302-levelC-worker.md)

---

## Timeline

| Time (UTC) | Event |
|------------|-------|
| 01:44:37 | Project created via API, task queued |
| 01:44:37 | Engineering message published to queue |
| 01:45:49 | Engineering-worker picked up task (delayed — had to restart worker due to stuck state from previous run) |
| 01:45:50 | GitHub repo `todo-api` created, secrets set |
| 01:45:54 | Worker spawn requested |
| 01:45:55 | Worker `dev-todo-api-3f2f114f` created, scaffold started |
| 01:46:07 | Scaffold commit `f043a711` — `feat: scaffold todo-api with modules: backend` |
| 01:46:08 | Scaffold verified (copier-answers + github-workflows present) |
| 01:46:10 | Claude Code agent starts working |
| 01:46:12 | CI run #22558088021 (scaffold commit) → **SUCCESS** |
| 01:57:39 | Implementation commit `20865aff` — `feat: implement Todo CRUD API with GET/POST/PATCH/DELETE /todos` |
| 01:57:45 | CI run #22558313718 (implementation) → **SUCCESS** |
| 01:59:53 | Engineering task completed |
| 02:00:05 | Deploy secrets configured (9 secrets), workflow dispatched |
| 02:00:06 | CI run #22558357007 (deploy.yml) starts |
| 02:01:55 | Deploy step "Deploy via SSH" failed — backend in crash loop (3 restarts) |
| 02:01:58 | Deploy task marked failed |

**Total duration**: ~17 minutes (scaffold ~20s, code gen ~12 min, CI ~2 min, deploy ~2 min)

## Verification

### Code generation: PASS
3 commits on main. Complete Todo CRUD API with spec-first workflow, unit tests, proper migrations. All generated through the spec-first pipeline.

### CI: PASS (first try!)
Unlike the previous run, both scaffold CI and implementation CI passed on first attempt. No CI fix cycles needed. This is a notable improvement.

### Deploy workflow: FAIL
- `deploy.yml` ran: SCP compose files → SSH deploy → containers started → backend entered crash loop.
- Deploy healthcheck correctly detected: `FATAL: /infra-backend-1 restarted 3 times — crash loop detected`

### Service reachable: FAIL
- Backend container in `Restarting` state on server.
- **Root cause** (same as previous run):
  ```
  File "/app/services/backend/src/controllers/debug.py", line 15, in <module>
      from shared.generated.events import publish_command_received
  ModuleNotFoundError: No module named 'shared.generated'
  ```
- The scaffold includes a `debug.py` controller that imports `shared.generated.events` — a module that doesn't exist because the events system isn't scaffolded for this project.
- Import chain: `main.py` → `app/__init__.py` → `api/router.py` → `routers/debug.py` → `controllers/debug.py` → boom.

### .env.prod: EMPTY
- The `.env.prod` file on the server was 0 bytes. This means all the database connection vars rely on defaults from compose anchors (`postgres`/`postgres`). DB was healthy, but this is fragile.

---

## Problems Found

### Problem 1: Backend crashes on deploy — `shared.generated.events` missing (RECURRING)

- **Type**: template
- **Severity**: critical
- **Description**: Same issue as the 2026-03-02 Level C run. The scaffold's `debug.py` controller imports `shared.generated.events.publish_command_received`, which doesn't exist when events aren't scaffolded.
- **Root cause**: The `debug.py` controller in the template unconditionally imports from `shared.generated.events`. This module is only created when events are configured in `events.yaml`. For projects without events, the import fails at startup.
- **Impact**: Service can't start at all. Every container restart hits the same import error.
- **This is the 2nd consecutive Level C run hitting this exact issue.**
- **Suggested fix**: Either (1) remove the events import from `debug.py` in the template, (2) guard it with `try/except ImportError`, or (3) always generate a stub `shared.generated.events` module even when no events are defined.

### Problem 2: .env.prod is empty on server

- **Type**: orchestrator
- **Severity**: major
- **Description**: The `.env.prod` file deployed to the server is 0 bytes. Critical environment variables (POSTGRES_USER, POSTGRES_PASSWORD, etc.) fall back to compose defaults.
- **Root cause**: The deploy workflow copies compose files but the `.env.prod` generation isn't populating it with actual values.
- **Impact**: Even if the backend could start, it would connect to Postgres with default credentials (`postgres`/`postgres`). For production deployments, this is insecure.
- **Suggested fix**: The deploy-worker should generate `.env.prod` with proper values from the project's encrypted secrets before dispatching the deploy workflow.

### Problem 3: Engineering-worker stuck after previous run cleanup

- **Type**: orchestrator
- **Severity**: major
- **Description**: After the previous E2E run's cleanup, the engineering-worker was stuck waiting for a worker completion signal that would never arrive. The new task sat in "queued" status until the worker was restarted.
- **Root cause**: The engineering-worker was blocking on a Redis stream wait for worker `dev-todo-api-379082dc` (from the previous run), which had already been killed during cleanup. The stream message never arrived, so the worker hung indefinitely.
- **Impact**: New tasks can't be processed until manual restart. This makes the system non-self-healing.
- **Suggested fix**: Add a timeout to the worker completion wait. If the worker container is no longer running and no completion message is received within N minutes, mark the task as failed and move on.

### Problem 4: Scaffold CI passes but should it run at all?

- **Type**: template
- **Severity**: minor
- **Description**: The scaffold commit triggers CI which runs successfully (no integration test failures this time — improvement from last run). However, running full CI on a bare scaffold with no business logic is wasted time (~2 min).
- **Note**: This is less severe than last run since CI now passes on scaffold. But it's still unnecessary compute.

---

## Worker Audit Highlights

The worker's audit report is thorough and well-organized. Key findings:

1. **Router generation gap** — routers are NOT auto-generated from specs. This is the biggest manual step in the spec-first workflow (~80 lines per domain).
2. **`response_list` validation error** — had to discover `list[ModelName]` syntax for list endpoints by reading framework source. No documentation.
3. **Generated protocol formatting** — inconsistent indentation in generated `protocols.py`.
4. **`make makemigrations` requires Docker** — no native mode available in worker container.
5. **All linters/tests pass** — `make lint` and `make tests` both clean after implementation.
6. **Spec-first workflow works well** — validate → generate → implement → test cycle is effective.

## Comparison with Previous Run (same day, earlier)

| Aspect | Previous Run | This Run |
|--------|-------------|----------|
| Scaffold CI | Failed (missing POSTGRES_USER) | **Passed** |
| Implementation CI | Failed → fixed → passed (1 fix cycle) | **Passed first try** |
| Deploy crash | `shared.generated.events` | `shared.generated.events` (same) |
| Code gen time | ~10 min | ~12 min |
| Total duration | ~20 min | ~17 min |
| CI fix cycles | 1 | 0 |

The CI reliability improved (no fix cycles needed), but the deploy crash is identical — confirming it's a template bug, not agent error.

## Summary

**Engineering pipeline is reliable**: scaffold → code gen → CI pass completed cleanly in ~15 minutes with zero CI fix cycles. The agent produced a complete CRUD API following the spec-first workflow, with proper tests and migrations.

**Deploy pipeline has a recurring blocker**: The `debug.py` template imports `shared.generated.events` which doesn't exist when events aren't configured. This is the same critical issue from the previous run and must be fixed in the service-template before Level C tests can pass.

**Orchestrator has a resilience gap**: The engineering-worker gets stuck after abnormal previous run termination, requiring manual restart. This needs a timeout/recovery mechanism.

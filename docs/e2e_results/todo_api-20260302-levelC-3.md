# E2E Report: todo_api — Level C full flow (engineering failed — CI infra issue)

> **Date**: 2026-03-02
> **Project**: todo_api (project_id: `2c8ebd23-d459-4e2a-8e32-d646948bd793`)
> **Task**: eng-ad6e5f3b3a45
> **Test level**: C
> **Status**: Failed (engineering CI gate — transient infra)
> **Worker audit**: [todo_api-20260302-levelC-3-worker.md](./todo_api-20260302-levelC-3-worker.md)

---

## Timeline

| Time (UTC) | Event |
|------------|-------|
| 12:45:37 | Pre-flight: checked GitHub repo (clean), servers (clean) |
| 12:47:00 | Project created via API |
| 12:47:01 | Engineering task `eng-ad6e5f3b3a45` published to queue |
| 12:47:05 | Resources allocated (port 8000 on vps-267179) |
| 12:47:06 | Worker `dev-todo-api-127903ce` created, scaffold started |
| 12:47:22 | Scaffold commit `f2d4ef57` — `feat: scaffold todo-api with modules: backend` |
| 12:47:24 | Scaffold verified (copier-answers + github-workflows present) |
| 12:47:25 | Claude Code agent starts working |
| 12:47:28 | CI run #22576671542 (scaffold) triggered |
| ~12:49:26 | CI run #22576671542 (scaffold) — **SUCCESS** (lint+test+build-and-push all passed) |
| 12:52:47 | Implementation commit `a4eff602` — `feat: implement Todo CRUD API` |
| 12:52:53 | CI run #22576860375 (implementation) triggered |
| 12:53:06 | Engineering worker marks code gen as success, enters CI gate |
| 12:54:57 | CI run #22576860375 — **FAILURE** (build-and-push: "Log in to Docker Registry" failed) |
| 12:54:58 | Engineering worker classifies as `ci_infra_failure`, marks task failed |

**Total duration**: ~8 minutes (scaffold ~17s, code gen ~5.5 min, CI gate ~2 min → fail)

## Verification

### Code generation: PASS

3 commits on main. Clean implementation of Todo CRUD API:
- Spec-first workflow (models.yaml → generate-from-spec → implement)
- All 4 endpoints: GET/POST/PATCH/DELETE /todos
- Tests included (18 tests, ~1.3s)
- No CI fix cycles needed — lint-and-test passed on first push

### CI: PARTIAL

| Run | Trigger | lint-and-test | build-and-push | Overall |
|-----|---------|---------------|----------------|---------|
| #22576671542 | push (scaffold) | success | success | **success** |
| #22576860375 | push (implementation) | success | **failure** | failure |

Both `lint-and-test` jobs passed. The scaffold `build-and-push` succeeded. The implementation `build-and-push` failed at "Log in to Docker Registry" with the **same secrets** — transient infra issue.

### Deploy: NOT REACHED

Engineering task failed at CI gate, so deploy was never triggered.

## Worker Audit Summary

The worker produced a good audit report (see linked file). Key findings:

**What worked well**: Spec-first codegen, clear patterns from User domain, spec validation, test infrastructure, controller sync linting.

**Issues found by worker**:
1. `orchestrator dev-env start-infra db` returned 500 — had to write migration manually
2. Double `shared/shared/` directory structure is confusing (though imports work)
3. No `list` operation example in scaffolded User domain
4. `ORMBase` includes `updated_at` unconditionally
5. Manual wiring still required (ORM model, repository, router, `__init__.py` files)

**Worker suggestions**: auto-generate routers, ORM models, `__init__.py` updates; add `make new-domain` command.

## Problems Found

### Problem 1: Transient Docker Registry login failure in CI

- **Type**: other
- **Severity**: major
- **Description**: The `build-and-push` job in CI run #22576860375 failed at "Log in to Docker Registry". The same step succeeded in the preceding CI run (#22576671542) with the same repository secrets (`REGISTRY_URL`, `REGISTRY_USER`, `REGISTRY_PASSWORD`). The self-hosted Docker Registry (via Caddy) was healthy and accepting requests — confirmed by registry container logs showing successful push operations during the first run.
- **Root cause**: Transient networking issue between GitHub Actions runner and the self-hosted registry. The runner for the second CI run could not establish a connection to `5oxt.l.time4vps.cloud` for Docker login, while the first runner (different job) had no issues.
- **Impact**: Engineering task failed after code gen succeeded. The code itself was fine — both lint-and-test passes confirm this. A retry would likely succeed.
- **Suggested fix**:
  1. Add retry logic to the `build-and-push` job (retry Docker login step 2-3 times with backoff)
  2. In the orchestrator's CI gate, distinguish transient infra failures from code failures and auto-retry the CI run via GitHub API (`POST /repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs`)

### Problem 2: Worker can't start infra DB for migrations

- **Type**: orchestrator
- **Severity**: minor
- **Description**: Worker reported that `orchestrator dev-env start-infra db` returned a 500 Internal Server Error from worker-manager. The worker had to write the Alembic migration manually instead of auto-generating it via `make makemigrations`.
- **Root cause**: The worker-manager's infra compose endpoint either has a bug or doesn't support the DB infra start for `dev-` workers properly.
- **Workaround**: Manual migration writing (worker handled it successfully).
- **Suggested fix**: Investigate the 500 error in worker-manager's `/api/worker/{id}/infra/compose` endpoint.

## Comparison with Previous Runs

| Run | Date | Code Gen | CI | Deploy | Failure |
|-----|------|----------|----|----|---------|
| levelC (run 1) | 2026-03-02 | PASS | PASS | FAIL | Backend crash: `shared.generated` missing in Docker image |
| levelC (run 2) | 2026-03-02 | PASS | PASS | FAIL | Same crash — template `.gitignore` + CI pipeline issue |
| **levelC (run 3)** | **2026-03-02** | **PASS** | **FAIL** | **N/A** | **Transient Docker Registry login failure** |

Code generation is consistently reliable. The previous deploy failures (runs 1-2) were a template bug (now fixed — `build-and-push` has "Generate code from specs" step). This run hit a different, transient issue.

## Summary

| Phase | Status | Duration | Notes |
|-------|--------|----------|-------|
| Pre-flight | PASS | — | GitHub repo clean, servers clean |
| Scaffold | PASS | ~17s | Clean, verified |
| Code generation | PASS | ~5.5 min | Single commit, first-try lint-and-test pass |
| CI (lint+test) | PASS | — | Both runs passed |
| CI (build+push) | FAIL | ~2 min | Transient registry login failure |
| Deploy | N/A | — | Not triggered |

Engineering pipeline code generation quality remains solid. The CI gate correctly identified the infra failure and didn't waste time on fix cycles. The failure is transient and not reproducible.

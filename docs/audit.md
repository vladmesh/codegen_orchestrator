# Code Audit

> **Date**: 2026-03-05
> **Scope**: full codebase (`services/`, `shared/`, `packages/`)
> **Previous audit**: 2026-03-04

## CI Health

✅ Last CI run passed (2026-03-04, `f6c0269`)

## Summary

- Dead code: 2 issues
- Code smells: 12 issues
- Security: 5 issues
- Test gaps: 88 files without unit tests

**Resolved since last audit:**
- ✅ #23 Extract infra_client + constants to shared — DONE
- ✅ #24 Fix critical getenv defaults — DONE

---

## Dead Code

| File | Issue | Action |
|------|-------|--------|
| `services/langgraph/src/list_repos.py` (72 LOC) | Standalone debug script, never imported, uses `print()` + `sys.path` hack | backlog #17 |
| `shared/schemas/tool_registry.py:88` | Redundant `pass` after `print()` | fix now |

## Code Smells

### Large files (>400 LOC)

| File | Lines | Status |
|------|------:|--------|
| `services/langgraph/src/workers/engineering_worker.py` | 1088 | backlog #18 |
| `shared/clients/github.py` | 986 | backlog #19 |
| `services/worker-manager/src/manager.py` | 828 | not tracked |
| `services/api/src/routers/rag.py` | 688 | not tracked |
| `services/langgraph/src/subgraphs/devops/nodes.py` | 644 | Ideas |
| `services/infra-service/src/provisioner/node.py` | 615 | not tracked |
| `services/telegram_bot/src/main.py` | 473 | Ideas |
| `services/langgraph/src/nodes/developer.py` | 466 | not tracked |
| `services/langgraph/src/subgraphs/devops/env_analyzer.py` | 465 | not tracked |
| `services/langgraph/src/clients/worker_spawner.py` | 418 | not tracked |
| `packages/worker-wrapper/src/worker_wrapper/wrapper.py` | 414 | not tracked |
| `services/scheduler/src/tasks/server_sync.py` | 411 | not tracked |

### Functions >50 LOC (top offenders)

| File | Function | LOC |
|------|----------|----:|
| `engineering_worker.py:588` | `process_engineering_job()` | 285 |
| `manager.py:434` | `create_worker_with_capabilities()` | 235 |
| `engineering_worker.py:257` | `_wait_for_ci_and_fix()` | 220 |
| `devops/nodes.py:419` | `run()` (DeployNode) | 220 |
| `developer.py:48` | `run()` | 203 |
| `engineering_worker.py:875` | `_handle_engineering_success()` | 201 |
| `worker_spawner.py:147` | `request_spawn()` | 168 |
| `server_sync.py:109` | `_sync_server_list()` | 131 |
| `provisioner/node.py:85` | `reinstall_and_provision()` | 115 |
| `server_sync.py:298` | `_check_provisioning_triggers()` | 114 |
| `wrapper.py:257` | `execute_agent()` | 108 |
| `env_analyzer.py:363` | `env_analyzer_run()` | 103 |

### Swallowed exceptions (except + pass)

| File | Line | Severity |
|------|------|----------|
| `services/worker-manager/src/events.py` | 85, 89, 93, 104 | major — 4 silenced exceptions in cleanup |
| `services/worker-manager/src/docker_ops.py` | 82 | minor |
| `services/worker-manager/src/main.py` | 116 | minor |

### Broad `except Exception:` clauses

23 instances total. Notable: `worker-manager/routers/compose.py:82,101`, `api/main.py:45`, `api/routers/projects.py:119`, `langgraph/po/consumer.py:186`, `telegram_bot/main.py:326`.

## Security

| File | Issue | Severity | Action |
|------|-------|----------|--------|
| `packages/orchestrator-cli/src/.../engineering.py:21` | `ORCHESTRATOR_USER_ID` defaults to `"unknown"` | major | not tracked |
| `packages/orchestrator-cli/src/.../deploy.py:21` | Same | major | not tracked |
| `packages/orchestrator-cli/src/.../respond.py:32` | Same | major | not tracked |
| `services/infra-service/src/provisioner/ansible_runner.py:99` | `subprocess.run` without input validation (noqa S603) | minor | reviewed OK |
| `services/infra-service/src/provisioner/recovery.py:73` | subprocess call | minor | reviewed OK |

**Resolved:** `shared/notifications.py` TELEGRAM_BOT_TOKEN/API_BASE_URL defaults — fixed in #24.

## Test Gaps

88 source files have no corresponding unit test. Top gaps by service:

| Service | Untested files | Notable gaps |
|---------|---------------:|--------------|
| langgraph | 31 | nodes/base, tools/*, workers/_base, config/*, clients/* |
| api | 25 | All routers except webhooks/delete/encryption/debug, all schemas |
| infra-service | 9 | All source files (0% coverage) |
| scheduler | 7 | health_checker, provisioner tasks, rag_summarizer |
| telegram_bot | 6 | handlers, main, middleware, keyboards |
| worker-wrapper | 5 | wrapper.py, runners/*, config |
| worker-manager | 4 | manager.py, agents/*, main |
| orchestrator-cli | 4 | client, commands/*, main |

Skipped tests:
- `tests/e2e/test_engineering_flow.py:84` — `@pytest.mark.skip` (full flow test)
- `tests/e2e/test_real_llm.py` — 4 `@pytest.mark.skipif` (conditional on env vars — OK)

## TODO/FIXME

| File | Line | Comment | Status |
|------|------|---------|--------|
| `shared/notifications.py` | 156 | `TODO: Add is_admin field filtering` | not tracked |
| `services/api/src/routers/servers.py` | 282 | `TODO: Trigger LangGraph provisioner node via queue/webhook` | not tracked |

---

## New Issues (triaged 2026-03-05)

1. **`ORCHESTRATOR_USER_ID` defaults to `"unknown"`** in 3 CLI command files — breaks audit trail. → backlog #29
2. **`worker-manager/src/manager.py` (828 LOC)** — 6 functions >50 LOC, split candidate. → Ideas
3. **infra-service: 0% unit test coverage** — 9 source files, 0 tests. → Ideas
4. **`services/langgraph/src/tests/test_architect_routing.py`** — test file inside `src/` instead of `tests/`. → backlog #17

## Already Tracked

| Issue | Backlog |
|-------|---------|
| Split engineering_worker.py | #18 |
| Split github.py | #19 |
| Dead code cleanup (list_repos.py) | #17 |
| Enable Ruff S110 + BLE001 | Ideas |
| Split Tier 2 large files | Ideas |

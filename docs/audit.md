# Code Audit

> **Date**: 2026-03-11
> **Scope**: full

## Summary
- Dead code: 2 issues
- Code smells: 15 issues
- Security: 0 issues
- Contract violations: 18 issues
- Convention violations: 3 issues
- Test gaps: 1 issue (infra-service)
- **Total**: 39 issues

## CI Health

✅ Last CI run passed (2026-03-10, commit `c487595`). [View run](https://github.com/project-factory-organization/codegen-orchestrator/actions/runs/22929734830).

---

## Dead Code

| File | Issue | Action |
|------|-------|--------|
| `services/scheduler/src/tasks/task_dispatcher.py:132` | `TODO: replace with proper project.internal flag when going to prod` | backlog |
| `services/api/src/routers/servers.py:363` | `TODO: Trigger LangGraph provisioner node via queue/webhook` | backlog — verify if still needed |

---

## Code Smells

15 files exceed the 400 LOC threshold:

| File | LOC | Action |
|------|-----|--------|
| `services/worker-manager/src/manager.py` | 889 | backlog — extract lifecycle methods |
| `services/langgraph/src/consumers/engineering.py` | 825 | backlog — extract helpers to module |
| `services/api/src/routers/rag.py` | 689 | backlog |
| `services/langgraph/src/subgraphs/devops/nodes.py` | 654 | backlog |
| `services/infra-service/src/provisioner/node.py` | 634 | backlog |
| `services/scheduler/src/tasks/task_dispatcher.py` | 577 | backlog |
| `services/api/src/routers/tasks.py` | 574 | backlog |
| `services/langgraph/src/consumers/deploy.py` | 565 | backlog |
| `services/langgraph/src/consumers/_ci_gate.py` | 531 | backlog |
| `services/telegram_bot/src/main.py` | 481 | backlog |
| `services/langgraph/src/subgraphs/devops/env_analyzer.py` | 467 | backlog |
| `services/langgraph/src/nodes/developer.py` | 466 | backlog |
| `services/langgraph/src/agents/po/tools.py` | 461 | backlog |
| `services/langgraph/src/clients/worker_spawner.py` | 428 | backlog |
| `services/scheduler/src/tasks/server_sync.py` | 411 | backlog |

`# noqa` suppressions that could be refactored:

| File:Line | Suppression | Action |
|-----------|-------------|--------|
| `langgraph/src/consumers/engineering.py:518` | `PLR0913` (too many args) | consider parameter object |
| `langgraph/src/consumers/engineering.py:606` | `PLR0913` (too many args) | consider parameter object |

---

## Security

No hardcoded secrets, tokens, or passwords found. ✅

`subprocess` calls reviewed — all pass controlled input (Ansible commands, Docker Compose, SSH key ops). No `shell=True` usage found. ✅

---

## Contract Violations

### Hardcoded status strings (high severity)

| File:Line | Violation | Should be |
|-----------|-----------|-----------|
| `scheduler/src/tasks/task_dispatcher.py:114` | `get_tasks_by_status("todo")` | `TaskStatus.TODO.value` |
| `scheduler/src/tasks/task_dispatcher.py:124` | `blocker.get("status") != "done"` | `TaskStatus.DONE.value` |
| `scheduler/src/tasks/task_dispatcher.py:150` | `"in_dev"` literal | `TaskStatus.IN_DEV.value` |
| `scheduler/src/tasks/task_dispatcher.py:167` | `"done"` literal | `TaskStatus.DONE.value` |
| `scheduler/src/tasks/task_dispatcher.py:211` | `transition_task(task_id, "in_dev", ...)` | `TaskStatus.IN_DEV.value` |
| `scheduler/src/tasks/task_dispatcher.py:227` | `get_stories_by_status("in_progress")` | `StoryStatus.IN_PROGRESS.value` |
| `scheduler/src/tasks/task_dispatcher.py:256` | `s == "done"` literal | `TaskStatus.DONE.value` |
| `scheduler/src/tasks/task_dispatcher.py:313,353,359` | `"created"`, `"in_progress"` story status literals | `StoryStatus.*.value` |
| `scheduler/src/tasks/task_dispatcher.py:423` | `get_tasks_by_status("failed")` | `TaskStatus.FAILED.value` |
| `scheduler/src/tasks/task_dispatcher.py:446-447` | `"backlog"`, `"todo"` literals | `TaskStatus.BACKLOG.value`, `.TODO.value` |
| `scheduler/src/tasks/task_dispatcher.py:465-469` | `"done"`, `"failed"`, `"cancelled"` literals | `TaskStatus.*.value` |
| `scheduler/src/tasks/task_dispatcher.py:503` | `get_tasks_by_status("in_dev")` | `TaskStatus.IN_DEV.value` |
| `scheduler/src/tasks/task_dispatcher.py:518` | `transition_task(task_id, "failed", ...)` | `TaskStatus.FAILED.value` |
| `langgraph/src/consumers/engineering.py:48-49` | `"done"`, `"in_ci"`, `"testing"` status literals | `TaskStatus.*.value` |
| `langgraph/src/consumers/engineering.py:703` | `_update_task_status(..., "done")` | `TaskStatus.DONE.value` |
| `langgraph/src/agents/architect/tools.py:77` | `"status": "todo"` | `TaskStatus.TODO.value` |
| `langgraph/src/consumers/architect.py:54` | `"status": "todo"` | `TaskStatus.TODO.value` |
| `scaffolder/src/consumer.py:59,131,135,142` | `"scaffolding"`, `"scaffolded"`, `"scaffold_failed"` | `ProjectStatus.*.value` |
| `scheduler/src/tasks/task_dispatcher.py:140` | `"draft"`, `"scaffolding"`, `"scaffold_failed"` project literals | `ProjectStatus.*.value` |
| `api/src/routers/webhooks.py:107` | `project.status != "active"` | `ProjectStatus.ACTIVE.value` |
| `langgraph/src/agents/po/tools.py:229-230` | `"draft"` project status literal | `ProjectStatus.DRAFT.value` |

### Direct Redis xadd/xread bypassing RedisStreamClient (medium severity)

| File:Line | Violation | Should be |
|-----------|-----------|-----------|
| `api/src/routers/webhooks.py:157` | direct `r.xadd()` | `redis_client.publish_message()` |
| `langgraph/src/consumers/engineering.py:774` | `redis.redis.xadd()` | `redis.publish_message()` |
| `worker-manager/src/consumer.py:154` | `client.redis.xadd()` | `client.publish_message()` |
| `worker-manager/src/events.py:146` | `self.redis.xadd()` | `publish_message()` |
| `scheduler/src/tasks/task_dispatcher.py:79` | `redis.xadd()` | `redis_client.publish_message()` |
| `scheduler/src/tasks/scaffold_trigger.py:79` | `redis_client.redis.xadd()` | `redis_client.publish_message()` |
| `langgraph/src/clients/worker_spawner.py:212,247,293,354,425` | `redis_client.xadd()` (5 instances) | `publish_message()` |
| `telegram_bot/src/main.py:166` | `redis.xread()` | consumer abstraction |

### Hardcoded queue/stream names (medium severity)

| File:Line | Violation | Should be |
|-----------|-----------|-----------|
| `langgraph/src/clients/provisioner_client.py:16` | `PROVISIONER_QUEUE = "provisioner:queue"` | `from shared.queues import PROVISIONER_QUEUE` |
| `langgraph/src/clients/worker_spawner.py:28` | `COMMAND_STREAM = "worker:commands"` | `from shared.queues import WORKER_COMMANDS` |
| `scheduler/src/tasks/task_dispatcher.py:50` | `WORKER_COMMANDS_STREAM = "worker:commands"` | `from shared.queues import WORKER_COMMANDS` |

### Hardcoded Redis key patterns (low-medium severity)

| File | Pattern | Action |
|------|---------|--------|
| `worker-manager/src/manager.py` | `f"worker:status:{worker_id}"`, `f"worker:meta:{worker_id}"`, `f"worker:error:{worker_id}"` (15+ instances) | centralize in `shared/redis/keys.py` |
| `langgraph/src/clients/worker_spawner.py:60` | `f"worker:status:{worker_id}"` | use shared constant |
| `worker-manager/src/routers/compose.py:43` | `f"worker:meta:{worker_id}"` | use shared constant |
| `worker-manager/src/events.py:153` | `f"worker:status:{worker_id}"` | use shared constant |

---

## Convention Violations

| File:Line | Violation | Rule |
|-----------|-----------|------|
| `services/infra-service/src/clients/api.py:20` | `os.getenv("API_BASE_URL", "http://api:8000")` | No env var defaults — fail fast with RuntimeError |
| `services/scheduler/src/tasks/server_sync.py:21` | `os.getenv("GHOST_SERVERS", "").split(",")` | Default empty string — acceptable but inconsistent |
| `services/scheduler/src/tasks/health_checker.py:9` | `os.getenv("HEALTH_CHECK_INTERVAL", "60")` | No env var defaults — use config or fail fast |

`print()` in `services/infra-service/ansible/inventory/api_inventory.py` — acceptable (Ansible dynamic inventory script requires stdout). ✅

No cross-service imports found. ✅
No `json.dumps(model.model_dump())` anti-pattern found. ✅

---

## Test Gaps

| Service | Source files | Test files | Coverage |
|---------|-------------|------------|----------|
| api | 38 | 20 | moderate |
| langgraph | 54 | 43 | good |
| scheduler | 11 | 8 | good |
| telegram_bot | 7 | 4 | moderate |
| scaffolder | 6 | 3 | moderate |
| worker-manager | 15 | 16 | good |
| **infra-service** | **9** | **1** | **critical gap** |

Skipped tests: `tests/e2e/test_engineering_flow.py:84` — intentional, OK.

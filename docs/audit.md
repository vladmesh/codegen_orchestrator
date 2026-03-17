# Code Audit

> **Date**: 2026-03-17
> **Scope**: full

## Summary
- Dead code: 1 issue
- Code smells: 10 issues
- Security: 0 issues
- Contract violations: 7 issues
- Missing DTOs & schema gaps: 5 issues
- Convention violations: 5 issues
- Test gaps: 1 issue (systemic — most services lack unit tests)

## CI Health

✅ Last CI run passed (2026-03-17) — commit `eb17038`, workflow "CI"

## Dead Code
| File | Issue | Action |
|------|-------|--------|
| `services/scheduler/src/tasks/task_dispatcher.py:136` | `# TODO: replace with proper project.internal flag when going to prod` — stale TODO, unclear if still relevant | review / backlog |

## Code Smells
| File | LOC | Issue | Action |
|------|-----|-------|--------|
| `services/worker-manager/src/manager.py` | 920 | File far exceeds 400 LOC limit | backlog — extract helpers |
| `services/langgraph/src/consumers/engineering.py` | 881 | File far exceeds 400 LOC limit | backlog — extract helpers |
| `services/langgraph/src/consumers/deploy.py` | 866 | File far exceeds 400 LOC limit | backlog — extract helpers |
| `services/scheduler/src/tasks/task_dispatcher.py` | 740 | File far exceeds 400 LOC limit | backlog — extract helpers |
| `services/api/src/routers/rag.py` | 689 | File far exceeds 400 LOC limit | backlog |
| `services/infra-service/src/provisioner/node.py` | 642 | File exceeds 400 LOC limit | backlog |
| `services/langgraph/src/subgraphs/devops/nodes.py` | 639 | File exceeds 400 LOC limit | backlog |
| `services/api/src/routers/tasks.py` | 625 | File exceeds 400 LOC limit | backlog |
| `services/langgraph/src/agents/po/tools.py` | 605 | File exceeds 400 LOC limit | backlog |
| `services/langgraph/src/nodes/developer.py` | 513 | File exceeds 400 LOC limit | backlog |

### noqa comments that could be fixed
| File:Line | Comment | Assessment |
|-----------|---------|------------|
| `services/telegram_bot/src/handlers.py:213` | `# noqa: PLR2004 — index into callback_data parts` | acceptable — magic number is an index |
| `services/api/src/routers/debug.py:65` | `# noqa: PLR2004` (pending > 100) | could use a named constant |
| `services/api/src/routers/debug.py:71` | `# noqa: S110` (bare except pass) | should log the error |
| `services/langgraph/src/consumers/engineering.py:682` | `# noqa: PLR0913` (too many args) | should refactor — extract params into a dataclass |
| `services/langgraph/src/subgraphs/devops/nodes.py:144` | `# noqa: PLR0911` (too many returns) | should refactor — extract lookup table |
| `services/langgraph/src/tools/specs.py:27,51,104,175` | `# noqa: PLR2004` (len != 2) | could use HTTPStatus or named const |
| `services/langgraph/src/tools/github.py:88` | `# noqa: PLR2004` (len != 2) | could use named const |

## Security
No hardcoded secrets, tokens, or passwords found. Subprocess calls use controlled inputs (Ansible runner, SSH manager, compose runner). The infra-service Ansible inventory uses `# noqa: S310` for `urllib.request.urlopen` — acceptable for an internal API call.

## Contract Violations
| File:Line | Violation | Should be | Severity |
|-----------|-----------|-----------|----------|
| `services/api/src/routers/webhooks.py:285` | `"status": "todo"` hardcoded in task creation dict | `TaskStatus.TODO.value` | high |
| `services/api/src/routers/webhooks.py:102` | Direct `r.xadd(DEPLOY_QUEUE, ...)` bypassing `RedisStreamClient` | `redis_client.publish_message()` | medium |
| `services/api/src/routers/webhooks.py:111,273` | `os.getenv("API_URL", "http://localhost:8000")` — env var with default | Fail fast with `RuntimeError` | medium |
| `services/api/src/routers/projects.py:303` | `["architect:queue", "scaffold:queue", "engineering:queue", "deploy:queue"]` hardcoded | Use `ARCHITECT_QUEUE`, `SCAFFOLD_QUEUE`, `ENGINEERING_QUEUE`, `DEPLOY_QUEUE` from `shared/queues.py` | medium |
| `services/langgraph/src/redis_publisher.py:38` | Direct `client.xadd(stream, {"data": data})` — bypasses `RedisStreamClient.publish_message()` | Use `publish_message()` or `publish()` | medium |
| `services/scheduler/src/tasks/task_dispatcher.py:54` + `services/langgraph/src/clients/story_worker_registry.py:19` | `STORY_WORKERS_KEY = "story:workers"` duplicated in two places | Centralize in `shared/queues.py` or a shared Redis keys module | low |
| `services/langgraph/src/consumers/engineering.py:127` | `{"backlog", "todo", "blocked"}` hardcoded status set | Use `TaskStatus` enum members | medium |

## Missing DTOs & Schema Gaps
| File:Line | Pattern | Suggested Fix | Severity |
|-----------|---------|---------------|----------|
| `services/langgraph/src/clients/api.py` (30+ methods) | Nearly all methods accept `payload: dict` and return `-> dict` | Create shared Pydantic DTOs for each entity (deployment, application, incident, etc.) | high |
| `services/scheduler/src/clients/api.py` (40+ methods) | All methods return `-> dict` with raw `resp.json()` | Validate through shared DTOs | high |
| `services/infra-service/src/clients/api.py:62-75` | `get_server() -> dict`, `update_server(payload: dict) -> dict` | Use shared server DTO | medium |
| `services/langgraph/src/agents/po/tools.py:125,243` | `repo_resp.json()["id"]`, `resp.json()["id"]` — raw indexing | Validate through DTO | medium |
| `services/telegram_bot/src/clients/api.py:49,53` | `return resp.json()` unvalidated | Validate through shared DTOs | medium |

## Convention Violations
| File:Line | Violation | Rule |
|-----------|-----------|------|
| `services/infra-service/ansible/inventory/api_inventory.py:30,73,75,77` | `print()` statements | Use structlog (though this is an Ansible dynamic inventory script — acceptable exception) |
| `services/api/src/routers/webhooks.py:111` | `os.getenv("API_URL", "http://localhost:8000")` | No env var defaults — fail fast |
| `services/api/src/routers/webhooks.py:273` | `os.getenv("API_URL", "http://localhost:8000")` (same, second occurrence) | No env var defaults — fail fast |
| `services/scheduler/src/tasks/server_sync.py:21` | `os.getenv("GHOST_SERVERS", "").split(",")` — empty string default | Borderline — empty default is reasonable for optional list, but deviates from convention |
| `services/api/src/routers/webhooks.py:285-289` | Inline dict literal passed as `json=task_data` to internal HTTP POST | Should use a shared DTO for task creation |

## Test Gaps

**150 test files exist** across all services. However, the naming convention doesn't follow a 1:1 src→test mapping — tests are organized by feature/behavior rather than by source file. This is acceptable.

Key areas with thin or missing coverage:
| Service | Coverage Gap |
|---------|-------------|
| `services/infra-service/` | Only 4 unit tests for a complex provisioning service (6 source files) |
| `services/telegram_bot/` | 4 unit tests for 6 source files; no test for `handlers.py`, `keyboards.py`, `middleware.py` |
| `services/api/` | No unit tests for many routers (`health`, `incidents`, `rag`, `resources`, `runs`, `servers`, `service_deployments`) — some covered by service tests |

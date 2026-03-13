# Code Audit

> **Date**: 2026-03-13
> **Scope**: full

## Summary
- Dead code: 0 issues
- Code smells: 12 issues
- Security: 0 issues
- Contract violations: 14 issues (3 fixed this audit)
- Missing DTOs & schema gaps: 8 issues
- Convention violations: 4 issues (2 fixed this audit)
- Test gaps: 1 issue

## CI Health
✅ Last CI run passed (2026-03-13)

## Dead Code
| File | Issue | Action |
|------|-------|--------|
| — | Ruff lint clean, no unused imports detected | — |

No dead code found. `make lint` passes clean.

## Code Smells
| File | LOC | Issue | Action |
|------|-----|-------|--------|
| `services/langgraph/src/consumers/engineering.py` | 981 | Far exceeds 400 LOC limit | backlog — split into modules |
| `services/worker-manager/src/manager.py` | 966 | Far exceeds 400 LOC limit | backlog — extract helpers |
| `services/langgraph/src/consumers/deploy.py` | 700 | Exceeds 400 LOC limit | backlog |
| `services/api/src/routers/rag.py` | 689 | Exceeds 400 LOC limit | backlog |
| `services/langgraph/src/subgraphs/devops/nodes.py` | 655 | Exceeds 400 LOC limit | backlog |
| `services/infra-service/src/provisioner/node.py` | 634 | Exceeds 400 LOC limit | backlog |
| `services/api/src/routers/tasks.py` | 625 | Exceeds 400 LOC limit | backlog |
| `services/langgraph/src/agents/po/tools.py` | 605 | Exceeds 400 LOC limit | backlog |
| `services/scheduler/src/tasks/task_dispatcher.py` | 599 | Exceeds 400 LOC limit | backlog |
| `services/langgraph/src/consumers/_ci_gate.py` | 531 | Exceeds 400 LOC limit | backlog |
| `services/langgraph/src/nodes/developer.py` | 504 | Exceeds 400 LOC limit | backlog |
| `services/telegram_bot/src/main.py` | 481 | Exceeds 400 LOC limit | backlog |

## Security
| File | Issue | Action |
|------|-------|--------|
| — | No hardcoded secrets, tokens, or passwords found | — |

`subprocess` calls in infra-service and worker-manager are expected for Ansible/Docker operations. All have appropriate `# noqa: S603` where needed.

## Contract Violations
| File:Line | Violation | Should be | Severity |
|-----------|-----------|-----------|----------|
| ~~`services/worker-manager/src/manager.py:97`~~ | ~~`"STARTING"` hardcoded string~~ | `WorkerStatus.STARTING` added to enum + used | ✅ fixed |
| ~~`services/langgraph/src/consumers/engineering.py:398`~~ | ~~`"failed"` hardcoded in run status patch~~ | Now uses `RunStatus.FAILED.value` | ✅ fixed |
| ~~`services/langgraph/src/consumers/engineering.py:438`~~ | ~~`"running"` hardcoded in run status patch~~ | Now uses `RunStatus.RUNNING.value` | ✅ fixed |
| `services/langgraph/src/subgraphs/devops/nodes.py:279` | `"running"` hardcoded deployment status | `RunStatus.RUNNING.value` or deployment enum | medium |
| `services/scheduler/src/tasks/task_dispatcher.py:290` | `"queued"` hardcoded run status | `RunStatus.QUEUED.value` | medium |
| `services/api/src/routers/webhooks.py:129` | `status="queued"` hardcoded | `RunStatus.QUEUED.value` | medium |
| `services/api/src/schemas/service_deployment.py:22` | `status: str = "running"` default | Use enum | medium |
| `services/api/src/schemas/server.py:21` | `status: str = "active"` default | `ServerStatus.ACTIVE.value` | medium |
| `services/langgraph/src/redis_publisher.py:38` | Direct `client.xadd()` bypassing `RedisStreamClient.publish_message()` | Use `publish_message()` or `publish()` | medium |
| `services/api/src/routers/webhooks.py:158` | Direct `r.xadd()` call | Use `RedisStreamClient.publish_message()` | medium |
| `services/worker-manager/src/events.py:148` | Direct `self.redis.xadd()` call | Use `RedisStreamClient.publish_message()` | medium |
| `services/langgraph/src/clients/worker_spawner.py:212,247,295,356,429` | Multiple direct `redis_client.xadd()` calls | Use `publish_message()` | medium |
| `services/scheduler/src/tasks/task_dispatcher.py:53` | Duplicated `STORY_WORKERS_KEY = "story:workers"` | Import from `shared/` or `story_worker_registry` | low |
| `services/worker-manager/src/manager.py` (many lines) | Hardcoded `f"worker:status:{worker_id}"`, `f"worker:meta:{worker_id}"`, `f"worker:error:{worker_id}"` patterns | Centralize as constants in `shared/` | low |

## Missing DTOs & Schema Gaps
| File:Line | Pattern | Suggested DTO | Severity |
|-----------|---------|---------------|----------|
| `services/langgraph/src/clients/api.py:120-150` | 6 methods with `payload: dict` params (`update_server`, `allocate_server_port`, `allocate_next_port`, `create_service_deployment`, `create_incident`, `update_incident`) | Create shared DTOs: `ServerUpdate`, `PortAllocationRequest`, `ServiceDeploymentCreate`, `IncidentCreate`, `IncidentUpdate` | high |
| `services/scheduler/src/clients/api.py:114,154,175` | `create_run(run_data: dict)`, `create_task(task_data: dict)`, `create_task_event(task_id, event: dict)` | `RunCreate`, `TaskCreate`, `TaskEventCreate` DTOs | high |
| `services/infra-service/src/clients/api.py:65` | `update_server(server_handle, payload: dict) -> dict` | Share `ServerUpdate` DTO with langgraph client | high |
| All API clients (`langgraph`, `scheduler`, `scaffolder`, `infra-service`, `telegram_bot`) | 40+ methods returning `resp.json()` as raw `dict` | Validate through shared DTOs (e.g., `ProjectDTO`, `ServerDTO`, `TaskDTO`) | medium |
| `services/langgraph/src/agents/po/tools.py:125` | `repo_resp.json()["id"]` — unvalidated response indexing | Validate through `RepositoryDTO` | medium |
| `services/langgraph/src/agents/po/tools.py:243` | `resp.json()["id"]` — unvalidated response indexing | Validate through `StoryDTO` | medium |
| `services/scaffolder/src/consumer.py:39` | `process_scaffold_job(job_data: dict)` | Type as `ScaffoldMessage` | medium |
| `services/langgraph/src/consumers/deploy.py:465` | `process_deploy_job(job_data: dict)` | Type as `DeployMessage` | medium |

## Convention Violations
| File:Line | Violation | Rule |
|-----------|-----------|------|
| ~~`services/infra-service/src/clients/api.py:22`~~ | ~~`os.getenv("API_BASE_URL", "http://api:8000")`~~ | Now fails fast with `RuntimeError` | ✅ fixed |
| `services/scheduler/src/tasks/health_checker.py:9` | `int(os.getenv("HEALTH_CHECK_INTERVAL", "60"))` | Acceptable — config tuning knob with sensible default, not a secret | ignore |
| `services/telegram_bot/src/handlers.py:212` | `# noqa: PLR2004` on index check | Use `http.HTTPStatus` or named constant |
| ~~`services/telegram_bot/src/handlers.py:375`~~ | ~~`# noqa: PLR2004` on status_code 400 check~~ | Now uses `HTTPStatus.BAD_REQUEST` | ✅ fixed |

Note: `services/infra-service/ansible/inventory/api_inventory.py` uses `print()` — acceptable since it's an Ansible dynamic inventory script (stdout is the interface).

Note: `services/scheduler/src/tasks/server_sync.py:21` — `GHOST_SERVERS = os.getenv("GHOST_SERVERS", "").split(",")` — borderline; empty-string default for a comma-separated list is a reasonable pattern (means "no ghost servers").

## Test Gaps
| File | Issue | Action |
|------|-------|--------|
| `services/admin-frontend/` | No tests directory | backlog — add frontend tests when UI stabilizes |

## noqa Audit
The following `# noqa` comments were reviewed:

| File:Line | Suppression | Verdict |
|-----------|------------|---------|
| `services/telegram_bot/src/main.py:38-45` | `E402` (import not at top) | OK — `setup_logging()` must run before imports |
| `services/infra-service/src/provisioner/ansible_runner.py:99` | `S603` (subprocess) | OK — Ansible runner, input is controlled |
| `services/langgraph/src/consumers/engineering.py:650,738` | `PLR0913` (too many args) | Could be refactored but low priority |
| `services/langgraph/src/subgraphs/devops/nodes.py:145` | `PLR0911` (too many returns) | Could extract but low priority |
| `services/langgraph/src/tools/specs.py:27,51,104,175` | `PLR2004` (magic value `2`) | OK — checking `len(parts) != 2` after split is idiomatic |
| `services/langgraph/src/tools/github.py:88` | `PLR2004` | Same — `len(parts) != 2` |
| ~~`services/api/src/routers/tasks.py:135,142`~~ | ~~`B904` (raise from)~~ | ✅ fixed — `raise ... from e`, noqa removed |
| ~~`services/api/src/routers/stories.py:52,59`~~ | ~~`B904`~~ | ✅ fixed — `raise ... from e`, noqa removed |
| ~~`services/api/src/routers/brainstorms.py:48,55`~~ | ~~`B904`~~ | ✅ fixed — `raise ... from e`, noqa removed |
| `services/api/src/routers/debug.py:65` | `PLR2004` | OK — threshold check |
| `services/api/src/routers/debug.py:71` | `S110` (try/except pass) | Acceptable in debug endpoint |
| `services/api/src/main.py:78` | `PLR2004` | Use `http.HTTPStatus` instead |

# Code Audit

> **Date**: 2026-03-22
> **Scope**: full

## Summary
- Dead code: 1 issue
- Code smells: 8 issues
- Security: 0 issues
- Contract violations: 12 issues
- Missing DTOs & schema gaps: 14 issues
- Convention violations: 9 issues
- Test gaps: 0 issues

**Total: 44 issues**

## CI Health

✅ Last CI run passed (2026-03-21, commit `e4803297`)

## Dead Code

| File | Issue | Action |
|------|-------|--------|
| `services/langgraph/src/redis_publisher.py` | `RedisPublisher` duplicates `RedisStreamClient.publish()` — only used by itself, no callers import `get_publisher()` outside module | backlog — remove and use `RedisStreamClient` |

## Code Smells

| File | LOC | Issue |
|------|-----|-------|
| `services/scheduler/src/tasks/supervisor.py` | 642 | > 400 LOC — orchestrates QA, engineering, deploy supervision |
| `services/api/src/routers/applications.py` | 546 | > 400 LOC |
| `services/telegram_bot/src/main.py` | 530 | > 400 LOC |
| `services/worker-manager/src/manager.py` | 527 | > 400 LOC |
| `services/langgraph/src/clients/worker_spawner.py` | 500 | > 400 LOC |
| `services/api/src/routers/servers.py` | 486 | > 400 LOC |
| `services/langgraph/src/consumers/_qa_runner.py` | 477 | > 400 LOC |
| `services/langgraph/src/subgraphs/devops/env_analyzer.py` | 467 | > 400 LOC |

## Security

No hardcoded secrets, tokens, or passwords found. `subprocess` calls in `infra-service` use controlled inputs (server handles, ansible commands). `# noqa: S603` on ansible_runner is acceptable — cmd is built from trusted config.

## Contract Violations

| File:Line | Violation | Should be | Severity |
|-----------|-----------|-----------|----------|
| `services/langgraph/src/consumers/story_context.py:51` | `status == "done"` hardcoded | `TaskStatus.DONE` (or `.value`) | high |
| `services/langgraph/src/clients/worker_spawner.py:315` | `status in ("success", "completed")` multi-guess | Single enum value from worker output contract | high |
| `services/langgraph/src/clients/worker_spawner.py:443` | `status in ("success", "completed")` multi-guess | Same as above — duplicated pattern | high |
| `services/scheduler/src/tasks/supervisor.py:633` | `"status": "todo"` hardcoded in raw dict | `TaskStatus.TODO.value` + use DTO | high |
| `services/scheduler/src/tasks/health_checker.py:57` | `{"active", "in_use", "ready"}` hardcoded strings | `{ServerStatus.ACTIVE, ServerStatus.IN_USE, ServerStatus.READY}` | medium |
| `services/langgraph/src/tools/allocator.py:153` | `s.status in ("active", "ready", "in_use")` | Use `ServerStatus` enum values | medium |
| `services/langgraph/src/tools/servers.py:43` | `s.status in ("ready", "in_use")` | Use `ServerStatus` enum values | medium |
| `services/scheduler/src/tasks/provisioner_result_listener.py:35` | `result.status in ("failed", "error")` | `BaseResult.status` is `Literal["success","failed","error","timeout"]` — ok but use enum or match the Literal | low |
| `services/scheduler/src/tasks/pr_poller.py:24` | `{StoryStatus.COMPLETED.value, "completed"}` redundant | Just `{StoryStatus.COMPLETED.value}` — `.value` IS `"completed"` | low |
| `services/infra-service/src/main.py:70` | `provisioning_result.get("status", "unknown")` | Use typed result, not raw dict `.get()` | medium |
| `services/langgraph/src/redis_publisher.py:38` | Direct `client.xadd(stream, {"data": data})` | Use `RedisStreamClient.publish_message()` | medium |
| `services/worker-manager/src/events.py:148` | Direct `self.redis.xadd(output_stream, {"data": ...})` | Use `RedisStreamClient` | medium |

## Missing DTOs & Schema Gaps

| File:Line | Pattern | Suggested DTO | Severity |
|-----------|---------|---------------|----------|
| `services/langgraph/src/clients/api.py:131` | `update_server(handle, payload: dict) -> dict` | `ServerUpdate` already exists — use it | high |
| `services/langgraph/src/clients/api.py:146` | `create_service_deployment(payload: dict) -> dict` | `ServiceDeploymentCreate` | high |
| `services/langgraph/src/clients/api.py:149` | `create_deployment(payload: dict) -> dict` | `DeploymentCreate` | high |
| `services/langgraph/src/clients/api.py:152` | `update_deployment(id, payload: dict) -> dict` | `DeploymentUpdate` | high |
| `services/langgraph/src/clients/api.py:164` | `create_application(payload: dict) -> dict` | `ApplicationCreate` | high |
| `services/langgraph/src/clients/api.py:167` | `update_application(id, payload: dict) -> dict` | `ApplicationUpdate` | high |
| `services/langgraph/src/clients/api.py:189` | `create_incident(payload: dict) -> dict` | `IncidentCreate` | high |
| `services/langgraph/src/clients/api.py:198` | `update_incident(id, payload: dict) -> dict` | `IncidentUpdate` | medium |
| `services/scheduler/src/clients/api.py:278` | `create_metrics_history(handle, metrics: dict) -> dict` | `MetricsHistoryCreate` | medium |
| `services/scheduler/src/clients/api.py:368` | `upsert_analytics_hourly(data: dict) -> dict` | `AnalyticsHourlyUpsert` | medium |
| `services/scheduler/src/clients/api.py:373` | `upsert_analytics_daily(data: dict) -> dict` | `AnalyticsDailyUpsert` | medium |
| `services/infra-service/src/clients/api.py:68` | `update_server(handle, payload: dict) -> dict` | Same `ServerUpdate` — duplicate of langgraph's | high |
| All API clients | `return resp.json()` (25+ occurrences) | Validate through DTOs: `SomeDTO.model_validate(resp.json())` | medium |
| `services/infra-service/src/provisioner/node.py:316` | `async def run(state: dict) -> dict` | `ProvisionerState` TypedDict or Pydantic model | medium |

## Convention Violations

| File:Line | Violation | Rule |
|-----------|-----------|------|
| `services/langgraph/src/consumers/_qa_runner.py:69` | `print(m.text)` in docstring code block | false positive — inside docstring |
| `services/infra-service/ansible/inventory/api_inventory.py:30,73,75,77` | `print()` in Ansible dynamic inventory script | Acceptable — Ansible inventory must print JSON to stdout |
| `services/infra-service/src/main.py:46` | `job_data.get("job_id") or job_data.get("request_id", "unknown")` | Fail fast — crash if key missing |
| `services/infra-service/src/main.py:47` | `job_data.get("server_handle", "")` | Fail fast — `server_handle` is required |
| `services/infra-service/src/main.py:59-60` | `job_data.get("is_recovery", False)` / `job_data.get("force_reinstall", False)` | Use typed DTO for job_data, not raw dict `.get()` |
| `services/telegram_bot/src/handlers.py:73-81` | Multiple `.get("field", "default")` on raw dicts | Validate through DTO — project data should be typed |
| `services/telegram_bot/src/handlers.py:109-113` | Multiple `.get("field", "default")` on raw dicts | Same — server data should be typed |
| `services/telegram_bot/src/handlers.py:257` | `# noqa: PLR2004` on `len(parts) > 2` | Not a magic number violation — `noqa` is unnecessary, could remove |
| `services/scheduler/src/tasks/server_sync.py:22` | `os.getenv("GHOST_SERVERS", "").split(",")` | Should fail fast or use Settings model |

## Glossary Violations

No glossary violations found. "Worker" usage is consistent with glossary (ephemeral Docker containers in worker-manager).

## Test Gaps

No skipped tests found. Test coverage structure is consistent with source modules.

## noqa Review

Most `# noqa` comments are justified:
- `E402` in telegram_bot (imports after `setup_logging()` call — required for log config)
- `F401` re-exports in `tools.py`, `rag.py` — intentional
- `S310` in Ansible inventory — `urllib.request` for Ansible compat, no `requests` available
- `S105` in `lk_auth.py`, `lk.py` — Redis key prefix / OAuth2 token_type, not passwords
- `PLW0603` for singleton globals — acceptable pattern

**Questionable:**
| File:Line | noqa | Issue |
|-----------|------|-------|
| `services/langgraph/src/subgraphs/devops/secret_resolver.py:135` | `PLR0911` (too many returns) | Could extract dispatch table — but low priority |
| `services/langgraph/src/consumers/engineering_result_handler.py:189` | `PLR0913` (too many args) | Could group into a dataclass/DTO — medium priority |

## Hardcoded Redis Key Patterns

Keys like `worker:status:{id}`, `worker:meta:{id}`, `worker:error:{id}`, `po:response:{id}`, `qa:inflight:{id}`, `scaffold:inflight:{id}`, `deploy:result:{id}` are scattered across services. These should be centralized in `shared/redis/keys.py` as helper functions. Low-medium severity — works but key pattern changes require multi-file grep.

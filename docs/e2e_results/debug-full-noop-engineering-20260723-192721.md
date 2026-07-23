# Debug: full-noop-engineering
**Time**: 2026-07-23T19:27:21.500387+00:00

## Context
- project_id: `63e778c9-d89f-4479-96ab-a4410ccaaa10`
- project_name: `live-te-63e778c9d89f447996aba4410ccaaa10`
- scaffold_status: `active`
- task_id: `task-197ed6ac`
- task_status: `failed`
- story_status: `in_progress`
- final_app_status: `None`
- deployed_url: `None`
- engineering_elapsed: `315`
- deploy_run_id: `None`
- deploy_head_sha: `None`
- deploy_run_status: `None`
- deploy_outcome: `None`
- deploy_error_details: `None`
- deploy_run_error: `None`
- deploy_outcome_error: `None`

## Environment contract
- scaffold @ `main`
  fragments: `["infra/env.contract.yaml", "services/backend/env.contract.yaml"]`
  entries: `["APP_ENV", "APP_NAME", "APP_SECRET_KEY", "ASYNC_DATABASE_URL", "BACKEND_IMAGE", "BACKEND_INSTALL_DEV_DEPS", "BACKEND_PORT", "BACKEND_REPLICAS", "COMPOSE_PROJECT_NAME", "DATABASE_URL", "DEBUG", "ENABLED_MODULES", "HOST_GID", "HOST_UID", "PORT", "POSTGRES_DB", "POSTGRES_HOST", "POSTGRES_HOST_PORT", "POSTGRES_PASSWORD", "POSTGRES_PORT", "POSTGRES_REQUIRE_SSL", "POSTGRES_USER", "REDIS_HOST_PORT", "REDIS_URL", "SQLALCHEMY_ASYNC_DRIVER", "SQLALCHEMY_SYNC_DRIVER", "TEST_REDIS_HOST", "TEST_REDIS_PORT", "TEST_REDIS_URL"]`
  merged_into_main: `None`

## CI failure evidence
- none captured

## scaffolder logs (last 30)
```
nch_protection", "lineno": 73}
scaffolder-1  | {"project_id": "63e778c9-d89f-4479-96ab-a4410ccaaa10", "repository_id": "repo-7c85f3a0", "event": "branch_protection_set", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-23T19:21:58.197023", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 231}
scaffolder-1  | HTTP Request: GET https://api.github.com/orgs/project-factory-organization/installation "HTTP/1.1 200 OK"
scaffolder-1  | HTTP Request: PATCH https://api.github.com/repos/project-factory-organization/live-te-63e778c9d89f447996aba4410ccaaa10 "HTTP/1.1 200 OK"
scaffolder-1  | {"owner": "project-factory-organization", "repo": "live-te-63e778c9d89f447996aba4410ccaaa10", "event": "repo_auto_merge_enabled", "level": "info", "logger": "shared.clients.github._actions", "timestamp": "2026-07-23T19:21:58.921717", "project_id": "63e778c9-d89f-4479-96ab-a4410ccaaa10", "service": "scaffolder", "func_name": "enable_repo_auto_merge", "lineno": 96}
scaffolder-1  | {"project_id": "63e778c9-d89f-4479-96ab-a4410ccaaa10", "repository_id": "repo-7c85f3a0", "event": "repo_auto_merge_enabled", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-23T19:21:58.921929", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 237}
scaffolder-1  | HTTP Request: PATCH http://api:8000/api/projects/63e778c9-d89f-4479-96ab-a4410ccaaa10 "HTTP/1.1 200 OK"
scaffolder-1  | {"project_id": "63e778c9-d89f-4479-96ab-a4410ccaaa10", "status": "active", "event": "project_status_updated", "level": "info", "logger": "src.clients.api", "timestamp": "2026-07-23T19:21:59.025265", "service": "scaffolder", "func_name": "update_project_status", "lineno": 71}
scaffolder-1  | {"project_id": "63e778c9-d89f-4479-96ab-a4410ccaaa10", "repository_id": "repo-7c85f3a0", "event": "scaffold_job_success", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-23T19:21:59.025404", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 242}
```

## engineering-worker logs (last 30)
```
t_id": "63e778c9-d89f-4479-96ab-a4410ccaaa10", "service": "engineering-worker", "correlation_id": "e83cb363-26eb-458f-b305-dbd41aeccefc", "request_id": "c0693ae0-d431-43e8-a6d8-32d94ebef201", "lineno": 348, "func_name": "_build_result_state"}
engineering-worker-1  | Failed to detach context
engineering-worker-1  | Traceback (most recent call last):
engineering-worker-1  |   File "/usr/local/lib/python3.12/site-packages/opentelemetry/context/__init__.py", line 155, in detach
engineering-worker-1  |     _RUNTIME_CONTEXT.detach(token)
engineering-worker-1  |   File "/usr/local/lib/python3.12/site-packages/opentelemetry/context/contextvars_context.py", line 53, in detach
engineering-worker-1  |     self._current_context.reset(token)
engineering-worker-1  | ValueError: <Token var=<ContextVar name='current_context' default={} at 0x7b7c2f9c3060> at 0x7b7c2be1f240> was created in a different Context
engineering-worker-1  | {"task_id": "eng-36fcebe44355", "errors": ["Development failed: Timeout after 300s waiting for worker to become ready"], "event": "engineering_job_failed_status", "level": "error", "logger": "__main__", "timestamp": "2026-07-23T19:27:16.612250", "story_id": "story-83ac754e", "project_id": "63e778c9-d89f-4479-96ab-a4410ccaaa10", "service": "engineering-worker", "correlation_id": "e83cb363-26eb-458f-b305-dbd41aeccefc", "request_id": "c0693ae0-d431-43e8-a6d8-32d94ebef201", "lineno": 316, "func_name": "process_engineering_job"}
engineering-worker-1  | {"planning_task_id": "task-197ed6ac", "new_status": "failed", "event": "task_status_updated", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-23T19:27:16.643964", "task_id": "eng-36fcebe44355", "story_id": "story-83ac754e", "project_id": "63e778c9-d89f-4479-96ab-a4410ccaaa10", "service": "engineering-worker", "correlation_id": "e83cb363-26eb-458f-b305-dbd41aeccefc", "request_id": "c0693ae0-d431-43e8-a6d8-32d94ebef201", "lineno": 64, "func_name": "_update_task_status"}
```

## scheduler logs (last 30)
```
ET http://api:8000/api/system-configs/supervisor.task_stuck_threshold_minutes "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/tasks/?status=failed "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=deploying "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=waiting_user_secret "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=testing "HTTP/1.1 200 OK"
scheduler-1  | {"tasks_dispatched": 0, "stories_completed": 0, "scaffolds_triggered": 0, "prs_merged": 0, "event": "dispatcher_cycle", "level": "info", "logger": "src.tasks.task_dispatcher", "timestamp": "2026-07-23T19:27:11.264021", "service": "scheduler", "func_name": "task_dispatcher_loop", "lineno": 369}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.dispatch_interval_seconds "HTTP/1.1 200 OK"
scheduler-1  | {"server_handle": "vps-273978", "server_ip": "185.81.166.84", "reason": "node_exporter_fetch_failed", "event": "server_unreachable", "level": "warning", "logger": "src.tasks.health_checker", "timestamp": "2026-07-23T19:27:13.612705", "service": "scheduler", "func_name": "_check_server", "lineno": 91}
scheduler-1  | HTTP Request: GET http://api:8000/api/incidents/?server_handle=vps-273978&incident_type=server_unreachable&status=detected "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/applications/ "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/health.metrics_cleanup_interval_seconds "HTTP/1.1 200 OK"
scheduler-1  | {"servers_checked": 1, "duration_sec": 10.1, "event": "health_check_cycle_complete", "level": "info", "logger": "src.tasks.health_checker", "timestamp": "2026-07-23T19:27:13.658246", "service": "scheduler", "func_name": "health_check_worker", "lineno": 323}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.rag_summarizer_poll_interval "HTTP/1.1 200 OK"
```

## deploy-worker logs (last 30)
```
    return await func(*args, **kwargs)
deploy-worker-1  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^
deploy-worker-1  |   File "/usr/local/lib/python3.12/site-packages/redis/asyncio/connection.py", line 1562, in get_connection
deploy-worker-1  |     await self.ensure_connection(connection)
deploy-worker-1  |   File "/usr/local/lib/python3.12/site-packages/redis/asyncio/connection.py", line 1603, in ensure_connection
deploy-worker-1  |     await connection.connect()
deploy-worker-1  |   File "/usr/local/lib/python3.12/site-packages/redis/asyncio/connection.py", line 350, in connect
deploy-worker-1  |     await self.retry.call_with_retry(
deploy-worker-1  |   File "/usr/local/lib/python3.12/site-packages/redis/asyncio/retry.py", line 81, in call_with_retry
deploy-worker-1  |     raise error
deploy-worker-1  |   File "/usr/local/lib/python3.12/site-packages/redis/asyncio/retry.py", line 69, in call_with_retry
deploy-worker-1  |     return await do()
deploy-worker-1  |            ^^^^^^^^^^
deploy-worker-1  |   File "/usr/local/lib/python3.12/site-packages/redis/asyncio/connection.py", line 407, in connect_check_health
deploy-worker-1  |     raise e
deploy-worker-1  | redis.exceptions.ConnectionError: Error -2 connecting to redis:6379. Name or service not known.
deploy-worker-1  | {"service": "deploy-worker", "log_format": "json", "log_level": "INFO", "event": "logging_initialized", "level": "info", "logger": "shared.log_config.config", "timestamp": "2026-07-23T17:51:25.242771", "func_name": "setup_logging", "lineno": 90}
deploy-worker-1  | {"event": "redis_connected", "level": "info", "logger": "shared.redis.client", "timestamp": "2026-07-23T17:51:25.243501", "service": "deploy-worker", "func_name": "connect", "lineno": 95}
deploy-worker-1  | {"consumer": "deploy-worker-1", "event": "deploy-worker_started", "level": "info", "logger": "src.consumers._base", "timestamp": "2026-07-23T17:51:25.244338", "service": "deploy-worker", "func_name": "run_queue_worker", "lineno": 141}
```

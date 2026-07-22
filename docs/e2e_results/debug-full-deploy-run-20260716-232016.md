# Debug: full-deploy-run
**Time**: 2026-07-16T23:20:16.624619+00:00

## Context
- project_id: `61e590be-ce16-46bf-9d1e-e63218a6ed53`
- project_name: `live-test-115ee2fb`
- scaffold_status: `active`
- task_id: `task-ccd19ec9`
- task_status: `done`
- story_status: `pr_review`
- final_app_status: `None`
- deployed_url: `None`
- engineering_elapsed: `20`
- deploy_run_id: `None`
- deploy_head_sha: `None`
- deploy_run_status: `None`
- deploy_outcome: `None`
- deploy_error_details: `None`
- deploy_run_error: `no deploy run with a merged head_sha appeared for story story-68d39696 within 420s`
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
9d1e-e63218a6ed53", "lineno": 73, "func_name": "update_branch_protection"}
scaffolder-1  | {"project_id": "61e590be-ce16-46bf-9d1e-e63218a6ed53", "repository_id": "repo-fd5ae789", "event": "branch_protection_set", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-16T23:12:25.856256", "service": "scaffolder", "lineno": 231, "func_name": "_process_full_mode"}
scaffolder-1  | HTTP Request: GET https://api.github.com/orgs/project-factory-organization/installation "HTTP/1.1 200 OK"
scaffolder-1  | HTTP Request: PATCH https://api.github.com/repos/project-factory-organization/live-test-115ee2fb "HTTP/1.1 200 OK"
scaffolder-1  | {"owner": "project-factory-organization", "repo": "live-test-115ee2fb", "event": "repo_auto_merge_enabled", "level": "info", "logger": "shared.clients.github._actions", "timestamp": "2026-07-16T23:12:26.876954", "service": "scaffolder", "project_id": "61e590be-ce16-46bf-9d1e-e63218a6ed53", "lineno": 96, "func_name": "enable_repo_auto_merge"}
scaffolder-1  | {"project_id": "61e590be-ce16-46bf-9d1e-e63218a6ed53", "repository_id": "repo-fd5ae789", "event": "repo_auto_merge_enabled", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-16T23:12:26.877160", "service": "scaffolder", "lineno": 237, "func_name": "_process_full_mode"}
scaffolder-1  | HTTP Request: PATCH http://api:8000/api/projects/61e590be-ce16-46bf-9d1e-e63218a6ed53 "HTTP/1.1 200 OK"
scaffolder-1  | {"project_id": "61e590be-ce16-46bf-9d1e-e63218a6ed53", "status": "active", "event": "project_status_updated", "level": "info", "logger": "src.clients.api", "timestamp": "2026-07-16T23:12:26.909986", "service": "scaffolder", "lineno": 71, "func_name": "update_project_status"}
scaffolder-1  | {"project_id": "61e590be-ce16-46bf-9d1e-e63218a6ed53", "repository_id": "repo-fd5ae789", "event": "scaffold_job_success", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-16T23:12:26.910185", "service": "scaffolder", "lineno": 242, "func_name": "_process_full_mode"}
```

## engineering-worker logs (last 30)
```
2d-b11e76a9f5cd", "lineno": 63, "func_name": "_update_task_status"}
engineering-worker-1  | {"planning_task_id": "task-b9d1504d", "new_status": "done", "event": "task_status_updated", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-16T23:15:36.809186", "project_id": "61e590be-ce16-46bf-9d1e-e63218a6ed53", "service": "engineering-worker", "correlation_id": "dcb54993-f929-4189-8e01-4b572908d49d", "story_id": "story-68d39696", "task_id": "eng-14703b0d4b27", "request_id": "b191951e-ffc4-416a-ba2d-b11e76a9f5cd", "lineno": 63, "func_name": "_update_task_status"}
engineering-worker-1  | {"task_id": "eng-14703b0d4b27", "planning_task_id": "task-b9d1504d", "skip_deploy": true, "effective_skip_deploy": true, "event": "deploy_decision", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-16T23:15:36.835846", "project_id": "61e590be-ce16-46bf-9d1e-e63218a6ed53", "service": "engineering-worker", "correlation_id": "dcb54993-f929-4189-8e01-4b572908d49d", "story_id": "story-68d39696", "request_id": "b191951e-ffc4-416a-ba2d-b11e76a9f5cd", "lineno": 311, "func_name": "handle_engineering_success"}
engineering-worker-1  | {"task_id": "eng-14703b0d4b27", "project_id": "61e590be-ce16-46bf-9d1e-e63218a6ed53", "event": "deploy_skipped", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-16T23:15:36.838161", "service": "engineering-worker", "correlation_id": "dcb54993-f929-4189-8e01-4b572908d49d", "story_id": "story-68d39696", "request_id": "b191951e-ffc4-416a-ba2d-b11e76a9f5cd", "lineno": 385, "func_name": "handle_engineering_success"}
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 0.83s.
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 2.06s.
engineering-worker-1  | Failed to export span batch due to timeout, max retries or shutdown.
```

## scheduler logs (last 30)
```
anup_interval_seconds "HTTP/1.1 200 OK"
scheduler-1  | {"servers_checked": 1, "duration_sec": 10.14, "event": "health_check_cycle_complete", "level": "info", "logger": "src.tasks.health_checker", "timestamp": "2026-07-16T23:19:50.766045", "service": "scheduler", "func_name": "health_check_worker", "lineno": 323}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.rag_summarizer_poll_interval "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/projects "HTTP/1.1 307 Temporary Redirect"
scheduler-1  | HTTP Request: GET http://api:8000/api/projects/ "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/tasks/?status=todo "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=in_progress "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=pr_review "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=pr_review "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=created "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=in_progress "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/tasks/?status=in_dev "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/tasks/?status=failed "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=deploying "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=testing "HTTP/1.1 200 OK"
scheduler-1  | {"tasks_dispatched": 0, "stories_completed": 0, "scaffolds_triggered": 0, "prs_merged": 0, "event": "dispatcher_cycle", "level": "info", "logger": "src.tasks.task_dispatcher", "timestamp": "2026-07-16T23:20:07.526978", "service": "scheduler", "func_name": "task_dispatcher_loop", "lineno": 234}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.dispatch_interval_seconds "HTTP/1.1 200 OK"
```

## deploy-worker logs (last 30)
```
deploy-worker-1  | {"service": "deploy-worker", "log_format": "json", "log_level": "INFO", "event": "logging_initialized", "level": "info", "logger": "shared.log_config.config", "timestamp": "2026-07-16T22:35:56.039724", "func_name": "setup_logging", "lineno": 90}
deploy-worker-1  | {"event": "redis_connected", "level": "info", "logger": "shared.redis.client", "timestamp": "2026-07-16T22:35:56.043666", "service": "deploy-worker", "func_name": "connect", "lineno": 95}
deploy-worker-1  | {"consumer": "deploy-worker-1", "event": "deploy-worker_started", "level": "info", "logger": "src.consumers._base", "timestamp": "2026-07-16T22:35:56.043853", "service": "deploy-worker", "func_name": "run_queue_worker", "lineno": 141}
```

# Debug: full-deploy-run
**Time**: 2026-07-16T22:56:34.760364+00:00

## Context
- project_id: `761dd5c0-86fc-4769-9928-ae4fff504acd`
- project_name: `live-test-27f09257`
- scaffold_status: `active`
- task_id: `task-e318297e`
- task_status: `done`
- story_status: `pr_review`
- final_app_status: `None`
- deployed_url: `None`
- engineering_elapsed: `60`
- deploy_run_id: `None`
- deploy_head_sha: `None`
- deploy_run_status: `None`
- deploy_outcome: `None`
- deploy_error_details: `None`
- deploy_run_error: `no deploy run with a merged head_sha appeared for story story-a81d31ce within 420s`
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
9928-ae4fff504acd", "lineno": 73, "func_name": "update_branch_protection"}
scaffolder-1  | {"project_id": "761dd5c0-86fc-4769-9928-ae4fff504acd", "repository_id": "repo-1373913b", "event": "branch_protection_set", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-16T22:48:01.608363", "service": "scaffolder", "lineno": 231, "func_name": "_process_full_mode"}
scaffolder-1  | HTTP Request: GET https://api.github.com/orgs/project-factory-organization/installation "HTTP/1.1 200 OK"
scaffolder-1  | HTTP Request: PATCH https://api.github.com/repos/project-factory-organization/live-test-27f09257 "HTTP/1.1 200 OK"
scaffolder-1  | {"owner": "project-factory-organization", "repo": "live-test-27f09257", "event": "repo_auto_merge_enabled", "level": "info", "logger": "shared.clients.github._actions", "timestamp": "2026-07-16T22:48:02.706039", "service": "scaffolder", "project_id": "761dd5c0-86fc-4769-9928-ae4fff504acd", "lineno": 96, "func_name": "enable_repo_auto_merge"}
scaffolder-1  | {"project_id": "761dd5c0-86fc-4769-9928-ae4fff504acd", "repository_id": "repo-1373913b", "event": "repo_auto_merge_enabled", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-16T22:48:02.706218", "service": "scaffolder", "lineno": 237, "func_name": "_process_full_mode"}
scaffolder-1  | HTTP Request: PATCH http://api:8000/api/projects/761dd5c0-86fc-4769-9928-ae4fff504acd "HTTP/1.1 200 OK"
scaffolder-1  | {"project_id": "761dd5c0-86fc-4769-9928-ae4fff504acd", "status": "active", "event": "project_status_updated", "level": "info", "logger": "src.clients.api", "timestamp": "2026-07-16T22:48:02.719056", "service": "scaffolder", "lineno": 71, "func_name": "update_project_status"}
scaffolder-1  | {"project_id": "761dd5c0-86fc-4769-9928-ae4fff504acd", "repository_id": "repo-1373913b", "event": "scaffold_job_success", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-16T22:48:02.719223", "service": "scaffolder", "lineno": 242, "func_name": "_process_full_mode"}
```

## engineering-worker logs (last 30)
```
ee-cc63976ad538", "lineno": 63, "func_name": "_update_task_status"}
engineering-worker-1  | {"planning_task_id": "task-4aa4e2ff", "new_status": "done", "event": "task_status_updated", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-16T22:53:04.686561", "project_id": "761dd5c0-86fc-4769-9928-ae4fff504acd", "service": "engineering-worker", "correlation_id": "642c3732-c2f9-46f4-9408-00d5fc453cc6", "story_id": "story-a81d31ce", "task_id": "eng-55b766b39774", "request_id": "a6e1fa8b-ba95-4033-98ee-cc63976ad538", "lineno": 63, "func_name": "_update_task_status"}
engineering-worker-1  | {"task_id": "eng-55b766b39774", "planning_task_id": "task-4aa4e2ff", "skip_deploy": true, "effective_skip_deploy": true, "event": "deploy_decision", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-16T22:53:04.718968", "project_id": "761dd5c0-86fc-4769-9928-ae4fff504acd", "service": "engineering-worker", "correlation_id": "642c3732-c2f9-46f4-9408-00d5fc453cc6", "story_id": "story-a81d31ce", "request_id": "a6e1fa8b-ba95-4033-98ee-cc63976ad538", "lineno": 311, "func_name": "handle_engineering_success"}
engineering-worker-1  | {"task_id": "eng-55b766b39774", "project_id": "761dd5c0-86fc-4769-9928-ae4fff504acd", "event": "deploy_skipped", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-16T22:53:04.720132", "service": "engineering-worker", "correlation_id": "642c3732-c2f9-46f4-9408-00d5fc453cc6", "story_id": "story-a81d31ce", "request_id": "a6e1fa8b-ba95-4033-98ee-cc63976ad538", "lineno": 385, "func_name": "handle_engineering_success"}
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 0.82s.
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 1.69s.
engineering-worker-1  | Failed to export span batch due to timeout, max retries or shutdown.
```

## scheduler logs (last 30)
```
illing.time4vps.com/api/server/273978 "HTTP/1.1 401 Unauthorized"
scheduler-1  | {"server_handle": "vps-273978", "error": "Client error '401 Unauthorized' for url 'https://billing.time4vps.com/api/server/273978'\nFor more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/401", "error_type": "HTTPStatusError", "event": "server_details_fetch_failed", "level": "warning", "logger": "src.tasks.server_sync", "timestamp": "2026-07-16T22:56:31.275730", "service": "scheduler", "func_name": "_sync_server_details", "lineno": 300}
scheduler-1  | {"updated_count": 0, "event": "server_details_sync_complete", "level": "info", "logger": "src.tasks.server_sync", "timestamp": "2026-07-16T22:56:31.275991", "service": "scheduler", "func_name": "_sync_server_details", "lineno": 308}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.provisioning_stuck_timeout_seconds "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.provisioning_trigger_cooldown_seconds "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers "HTTP/1.1 307 Temporary Redirect"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers/ "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers "HTTP/1.1 307 Temporary Redirect"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers/ "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/incidents/active "HTTP/1.1 200 OK"
scheduler-1  | {"servers_discovered": 0, "servers_updated": 0, "servers_missing": 0, "details_updated": 0, "triggers_published": 0, "incidents_resolved": 0, "duration_sec": 0.38, "event": "server_sync_complete", "level": "info", "logger": "src.tasks.server_sync", "timestamp": "2026-07-16T22:56:31.342058", "service": "scheduler", "func_name": "sync_servers_worker", "lineno": 107}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.server_sync_interval "HTTP/1.1 200 OK"
```

## deploy-worker logs (last 30)
```
deploy-worker-1  | {"service": "deploy-worker", "log_format": "json", "log_level": "INFO", "event": "logging_initialized", "level": "info", "logger": "shared.log_config.config", "timestamp": "2026-07-16T22:35:56.039724", "func_name": "setup_logging", "lineno": 90}
deploy-worker-1  | {"event": "redis_connected", "level": "info", "logger": "shared.redis.client", "timestamp": "2026-07-16T22:35:56.043666", "service": "deploy-worker", "func_name": "connect", "lineno": 95}
deploy-worker-1  | {"consumer": "deploy-worker-1", "event": "deploy-worker_started", "level": "info", "logger": "src.consumers._base", "timestamp": "2026-07-16T22:35:56.043853", "service": "deploy-worker", "func_name": "run_queue_worker", "lineno": 141}
```

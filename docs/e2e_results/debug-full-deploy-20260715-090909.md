# Debug: full-deploy
**Time**: 2026-07-15T09:09:09.535853+00:00

## Context
- project_id: `d816421c-7170-4667-bad8-69dbee9f82b8`
- project_name: `live-test-801a8aa1`
- scaffold_status: `active`
- task_id: `task-a1560a34`
- task_status: `done`
- story_status: `pr_review`
- final_app_status: `None`
- deployed_url: `None`
- engineering_elapsed: `55`

## scaffolder logs (last 30)
```
ice": "scaffolder", "func_name": "update_branch_protection", "lineno": 68}
scaffolder-1  | {"project_id": "d816421c-7170-4667-bad8-69dbee9f82b8", "repository_id": "repo-75f1fa00", "event": "branch_protection_set", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-15T09:00:46.361781", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 231}
scaffolder-1  | HTTP Request: GET https://api.github.com/orgs/project-factory-organization/installation "HTTP/1.1 200 OK"
scaffolder-1  | HTTP Request: PATCH https://api.github.com/repos/project-factory-organization/live-test-801a8aa1 "HTTP/1.1 200 OK"
scaffolder-1  | {"owner": "project-factory-organization", "repo": "live-test-801a8aa1", "event": "repo_auto_merge_enabled", "level": "info", "logger": "shared.clients.github._actions", "timestamp": "2026-07-15T09:00:47.318026", "project_id": "d816421c-7170-4667-bad8-69dbee9f82b8", "service": "scaffolder", "func_name": "enable_repo_auto_merge", "lineno": 91}
scaffolder-1  | {"project_id": "d816421c-7170-4667-bad8-69dbee9f82b8", "repository_id": "repo-75f1fa00", "event": "repo_auto_merge_enabled", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-15T09:00:47.318237", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 237}
scaffolder-1  | HTTP Request: PATCH http://api:8000/api/projects/d816421c-7170-4667-bad8-69dbee9f82b8 "HTTP/1.1 200 OK"
scaffolder-1  | {"project_id": "d816421c-7170-4667-bad8-69dbee9f82b8", "status": "active", "event": "project_status_updated", "level": "info", "logger": "src.clients.api", "timestamp": "2026-07-15T09:00:47.350240", "service": "scaffolder", "func_name": "update_project_status", "lineno": 71}
scaffolder-1  | {"project_id": "d816421c-7170-4667-bad8-69dbee9f82b8", "repository_id": "repo-75f1fa00", "event": "scaffold_job_success", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-15T09:00:47.351985", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 242}
```

## engineering-worker logs (last 30)
```
d8-69dbee9f82b8", "lineno": 63, "func_name": "_update_task_status"}
engineering-worker-1  | {"planning_task_id": "task-070123c2", "new_status": "done", "event": "task_status_updated", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-15T09:08:09.006391", "correlation_id": "5130ed73-e1ca-4c51-a9a3-aeeb5e335996", "service": "engineering-worker", "request_id": "d7987e98-e6ad-4549-bac5-6e84b9fbca64", "task_id": "eng-7bbccbfcfe3c", "story_id": "story-6014eed7", "project_id": "d816421c-7170-4667-bad8-69dbee9f82b8", "lineno": 63, "func_name": "_update_task_status"}
engineering-worker-1  | {"task_id": "eng-7bbccbfcfe3c", "planning_task_id": "task-070123c2", "skip_deploy": true, "effective_skip_deploy": true, "event": "deploy_decision", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-15T09:08:09.021388", "correlation_id": "5130ed73-e1ca-4c51-a9a3-aeeb5e335996", "service": "engineering-worker", "request_id": "d7987e98-e6ad-4549-bac5-6e84b9fbca64", "story_id": "story-6014eed7", "project_id": "d816421c-7170-4667-bad8-69dbee9f82b8", "lineno": 311, "func_name": "handle_engineering_success"}
engineering-worker-1  | {"task_id": "eng-7bbccbfcfe3c", "project_id": "d816421c-7170-4667-bad8-69dbee9f82b8", "event": "deploy_skipped", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-15T09:08:09.021629", "correlation_id": "5130ed73-e1ca-4c51-a9a3-aeeb5e335996", "service": "engineering-worker", "request_id": "d7987e98-e6ad-4549-bac5-6e84b9fbca64", "story_id": "story-6014eed7", "lineno": 385, "func_name": "handle_engineering_success"}
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 0.89s.
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 2.07s.
engineering-worker-1  | Failed to export span batch due to timeout, max retries or shutdown.
```

## scheduler logs (last 30)
```
ystem-configs/scheduler.provisioning_trigger_cooldown_seconds "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers "HTTP/1.1 307 Temporary Redirect"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers/ "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers "HTTP/1.1 307 Temporary Redirect"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers/ "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/incidents/active "HTTP/1.1 200 OK"
scheduler-1  | {"servers_discovered": 0, "servers_updated": 0, "servers_missing": 0, "details_updated": 0, "triggers_published": 0, "incidents_resolved": 0, "duration_sec": 0.42, "event": "server_sync_complete", "level": "info", "logger": "src.tasks.server_sync", "timestamp": "2026-07-15T09:08:42.781928", "service": "scheduler", "lineno": 107, "func_name": "sync_servers_worker"}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.server_sync_interval "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.rag_summarizer_poll_interval "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/projects "HTTP/1.1 307 Temporary Redirect"
scheduler-1  | HTTP Request: GET http://api:8000/api/projects/ "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/tasks/?status=todo "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=in_progress "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=pr_review "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/repositories/?project_id=d816421c-7170-4667-bad8-69dbee9f82b8 "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET https://api.github.com/repos/project-factory-organization/live-test-801a8aa1/installation "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: POST https://api.github.com/app/installations/100979986/access_tokens "HTTP/1.1 201 Created"
```

## deploy-worker logs (last 30)
```
deploy-worker-1  | {"service": "deploy-worker", "log_format": "json", "log_level": "INFO", "event": "logging_initialized", "level": "info", "logger": "shared.log_config.config", "timestamp": "2026-07-15T08:21:40.426936", "lineno": 90, "func_name": "setup_logging"}
deploy-worker-1  | {"event": "redis_connected", "level": "info", "logger": "shared.redis.client", "timestamp": "2026-07-15T08:21:40.427628", "service": "deploy-worker", "lineno": 95, "func_name": "connect"}
deploy-worker-1  | {"consumer": "deploy-worker-1", "event": "deploy-worker_started", "level": "info", "logger": "src.consumers._base", "timestamp": "2026-07-15T08:21:40.427799", "service": "deploy-worker", "lineno": 140, "func_name": "run_queue_worker"}
```

# Debug: full-deploy
**Time**: 2026-07-15T10:01:01.345138+00:00

## Context
- project_id: `ad84b3f3-0bad-4406-a119-2d9fb1fafb68`
- project_name: `live-test-89f8878a`
- scaffold_status: `active`
- task_id: `task-16b7070e`
- task_status: `done`
- story_status: `pr_review`
- final_app_status: `None`
- deployed_url: `None`
- engineering_elapsed: `30`

## scaffolder logs (last 30)
```
ice": "scaffolder", "func_name": "update_branch_protection", "lineno": 68}
scaffolder-1  | {"project_id": "ad84b3f3-0bad-4406-a119-2d9fb1fafb68", "repository_id": "repo-deb5fbed", "event": "branch_protection_set", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-15T09:52:59.779326", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 231}
scaffolder-1  | HTTP Request: GET https://api.github.com/orgs/project-factory-organization/installation "HTTP/1.1 200 OK"
scaffolder-1  | HTTP Request: PATCH https://api.github.com/repos/project-factory-organization/live-test-89f8878a "HTTP/1.1 200 OK"
scaffolder-1  | {"owner": "project-factory-organization", "repo": "live-test-89f8878a", "event": "repo_auto_merge_enabled", "level": "info", "logger": "shared.clients.github._actions", "timestamp": "2026-07-15T09:53:00.446415", "project_id": "ad84b3f3-0bad-4406-a119-2d9fb1fafb68", "service": "scaffolder", "func_name": "enable_repo_auto_merge", "lineno": 91}
scaffolder-1  | {"project_id": "ad84b3f3-0bad-4406-a119-2d9fb1fafb68", "repository_id": "repo-deb5fbed", "event": "repo_auto_merge_enabled", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-15T09:53:00.446603", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 237}
scaffolder-1  | HTTP Request: PATCH http://api:8000/api/projects/ad84b3f3-0bad-4406-a119-2d9fb1fafb68 "HTTP/1.1 200 OK"
scaffolder-1  | {"project_id": "ad84b3f3-0bad-4406-a119-2d9fb1fafb68", "status": "active", "event": "project_status_updated", "level": "info", "logger": "src.clients.api", "timestamp": "2026-07-15T09:53:00.459888", "service": "scaffolder", "func_name": "update_project_status", "lineno": 71}
scaffolder-1  | {"project_id": "ad84b3f3-0bad-4406-a119-2d9fb1fafb68", "repository_id": "repo-deb5fbed", "event": "scaffold_job_success", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-15T09:53:00.460390", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 242}
```

## engineering-worker logs (last 30)
```
19-2d9fb1fafb68", "lineno": 63, "func_name": "_update_task_status"}
engineering-worker-1  | {"planning_task_id": "task-3a80f62c", "new_status": "done", "event": "task_status_updated", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-15T10:00:36.483073", "correlation_id": "a87c8946-a1d3-4aa0-9725-95e1f181cec8", "service": "engineering-worker", "request_id": "fee3f940-7582-4b3d-80ec-08cc078b5062", "task_id": "eng-1a5b5557f07b", "story_id": "story-49be4026", "project_id": "ad84b3f3-0bad-4406-a119-2d9fb1fafb68", "lineno": 63, "func_name": "_update_task_status"}
engineering-worker-1  | {"task_id": "eng-1a5b5557f07b", "planning_task_id": "task-3a80f62c", "skip_deploy": true, "effective_skip_deploy": true, "event": "deploy_decision", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-15T10:00:36.503059", "correlation_id": "a87c8946-a1d3-4aa0-9725-95e1f181cec8", "service": "engineering-worker", "request_id": "fee3f940-7582-4b3d-80ec-08cc078b5062", "story_id": "story-49be4026", "project_id": "ad84b3f3-0bad-4406-a119-2d9fb1fafb68", "lineno": 311, "func_name": "handle_engineering_success"}
engineering-worker-1  | {"task_id": "eng-1a5b5557f07b", "project_id": "ad84b3f3-0bad-4406-a119-2d9fb1fafb68", "event": "deploy_skipped", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-15T10:00:36.503303", "correlation_id": "a87c8946-a1d3-4aa0-9725-95e1f181cec8", "service": "engineering-worker", "request_id": "fee3f940-7582-4b3d-80ec-08cc078b5062", "story_id": "story-49be4026", "lineno": 385, "func_name": "handle_engineering_success"}
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 1.11s.
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 1.96s.
engineering-worker-1  | Failed to export span batch due to timeout, max retries or shutdown.
```

## scheduler logs (last 30)
```
api/servers "HTTP/1.1 307 Temporary Redirect"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers/ "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers "HTTP/1.1 307 Temporary Redirect"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers/ "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/incidents/active "HTTP/1.1 200 OK"
scheduler-1  | {"servers_discovered": 0, "servers_updated": 0, "servers_missing": 0, "details_updated": 0, "triggers_published": 0, "incidents_resolved": 0, "duration_sec": 0.27, "event": "server_sync_complete", "level": "info", "logger": "src.tasks.server_sync", "timestamp": "2026-07-15T10:00:59.685837", "service": "scheduler", "lineno": 107, "func_name": "sync_servers_worker"}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.server_sync_interval "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/projects "HTTP/1.1 307 Temporary Redirect"
scheduler-1  | HTTP Request: GET http://api:8000/api/projects/ "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/tasks/?status=todo "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=in_progress "HTTP/1.1 200 OK"
scheduler-1  | {"in_progress_stories": 1, "event": "complete_stories_check", "level": "info", "logger": "src.tasks.story_completion", "timestamp": "2026-07-15T10:01:00.795899", "service": "scheduler", "lineno": 119, "func_name": "complete_stories"}
scheduler-1  | HTTP Request: GET http://api:8000/api/tasks/?story_id=story-49be4026 "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/repositories/?project_id=ad84b3f3-0bad-4406-a119-2d9fb1fafb68 "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET https://api.github.com/repos/project-factory-organization/live-test-89f8878a/installation "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: POST https://api.github.com/app/installations/100979986/access_tokens "HTTP/1.1 201 Created"
```

## deploy-worker logs (last 30)
```
deploy-worker-1  | {"service": "deploy-worker", "log_format": "json", "log_level": "INFO", "event": "logging_initialized", "level": "info", "logger": "shared.log_config.config", "timestamp": "2026-07-15T08:21:40.426936", "lineno": 90, "func_name": "setup_logging"}
deploy-worker-1  | {"event": "redis_connected", "level": "info", "logger": "shared.redis.client", "timestamp": "2026-07-15T08:21:40.427628", "service": "deploy-worker", "lineno": 95, "func_name": "connect"}
deploy-worker-1  | {"consumer": "deploy-worker-1", "event": "deploy-worker_started", "level": "info", "logger": "src.consumers._base", "timestamp": "2026-07-15T08:21:40.427799", "service": "deploy-worker", "lineno": 140, "func_name": "run_queue_worker"}
```

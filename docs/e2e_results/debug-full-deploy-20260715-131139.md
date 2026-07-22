# Debug: full-deploy
**Time**: 2026-07-15T13:11:39.914269+00:00

## Context
- project_id: `78e9c64c-6acc-4c7e-87a9-bd53a20e8d14`
- project_name: `live-test-39113a4c`
- scaffold_status: `active`
- task_id: `task-ff321cd7`
- task_status: `done`
- story_status: `pr_review`
- final_app_status: `None`
- deployed_url: `None`
- engineering_elapsed: `15`

## CI failure evidence
- fix_task_id: `task-5fa960f9`
  run_id: `29417716626`
  head_sha: `9fa487f3377eb007b9894a06917791d16bf70660`
  fingerprint: `fe5ea3d3eb0cbbec`
  failed_jobs: `[{"failed_steps": ["Run integration tests"], "name": "lint-and-test"}]`
- fix_task_id: `task-9468c809`
  run_id: `29417873217`
  head_sha: `51e5fc8557fa5fc08a83a6cddcb01df7806fb33e`
  fingerprint: `fe5ea3d3eb0cbbec`
  failed_jobs: `[{"failed_steps": ["Run integration tests"], "name": "lint-and-test"}]`

## scaffolder logs (last 30)
```
ice": "scaffolder", "func_name": "update_branch_protection", "lineno": 68}
scaffolder-1  | {"project_id": "78e9c64c-6acc-4c7e-87a9-bd53a20e8d14", "repository_id": "repo-f8fd5edf", "event": "branch_protection_set", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-15T13:03:56.520946", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 231}
scaffolder-1  | HTTP Request: GET https://api.github.com/orgs/project-factory-organization/installation "HTTP/1.1 200 OK"
scaffolder-1  | HTTP Request: PATCH https://api.github.com/repos/project-factory-organization/live-test-39113a4c "HTTP/1.1 200 OK"
scaffolder-1  | {"owner": "project-factory-organization", "repo": "live-test-39113a4c", "event": "repo_auto_merge_enabled", "level": "info", "logger": "shared.clients.github._actions", "timestamp": "2026-07-15T13:03:57.267319", "project_id": "78e9c64c-6acc-4c7e-87a9-bd53a20e8d14", "service": "scaffolder", "func_name": "enable_repo_auto_merge", "lineno": 91}
scaffolder-1  | {"project_id": "78e9c64c-6acc-4c7e-87a9-bd53a20e8d14", "repository_id": "repo-f8fd5edf", "event": "repo_auto_merge_enabled", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-15T13:03:57.267482", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 237}
scaffolder-1  | HTTP Request: PATCH http://api:8000/api/projects/78e9c64c-6acc-4c7e-87a9-bd53a20e8d14 "HTTP/1.1 200 OK"
scaffolder-1  | {"project_id": "78e9c64c-6acc-4c7e-87a9-bd53a20e8d14", "status": "active", "event": "project_status_updated", "level": "info", "logger": "src.clients.api", "timestamp": "2026-07-15T13:03:57.282659", "service": "scaffolder", "func_name": "update_project_status", "lineno": 71}
scaffolder-1  | {"project_id": "78e9c64c-6acc-4c7e-87a9-bd53a20e8d14", "repository_id": "repo-f8fd5edf", "event": "scaffold_job_success", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-15T13:03:57.282846", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 242}
```

## engineering-worker logs (last 30)
```
a9-bd53a20e8d14", "lineno": 63, "func_name": "_update_task_status"}
engineering-worker-1  | {"planning_task_id": "task-9468c809", "new_status": "done", "event": "task_status_updated", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-15T13:09:07.544929", "correlation_id": "2ba88d01-0627-4820-8fda-80d5062f55cf", "service": "engineering-worker", "request_id": "44eedf8f-936d-44a7-9fa2-2c4f21363be1", "task_id": "eng-c329128faba2", "story_id": "story-35e41911", "project_id": "78e9c64c-6acc-4c7e-87a9-bd53a20e8d14", "lineno": 63, "func_name": "_update_task_status"}
engineering-worker-1  | {"task_id": "eng-c329128faba2", "planning_task_id": "task-9468c809", "skip_deploy": true, "effective_skip_deploy": true, "event": "deploy_decision", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-15T13:09:07.558568", "correlation_id": "2ba88d01-0627-4820-8fda-80d5062f55cf", "service": "engineering-worker", "request_id": "44eedf8f-936d-44a7-9fa2-2c4f21363be1", "story_id": "story-35e41911", "project_id": "78e9c64c-6acc-4c7e-87a9-bd53a20e8d14", "lineno": 311, "func_name": "handle_engineering_success"}
engineering-worker-1  | {"task_id": "eng-c329128faba2", "project_id": "78e9c64c-6acc-4c7e-87a9-bd53a20e8d14", "event": "deploy_skipped", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-15T13:09:07.558804", "correlation_id": "2ba88d01-0627-4820-8fda-80d5062f55cf", "service": "engineering-worker", "request_id": "44eedf8f-936d-44a7-9fa2-2c4f21363be1", "story_id": "story-35e41911", "lineno": 385, "func_name": "handle_engineering_success"}
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 1.19s.
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 1.75s.
engineering-worker-1  | Failed to export span batch due to timeout, max retries or shutdown.
```

## scheduler logs (last 30)
```
ption": "Traceback (most recent call last):\n  File \"/app/src/tasks/server_sync.py\", line 124, in _sync_server_list\n    api_servers = await client.get_servers()\n                  ^^^^^^^^^^^^^^^^^^^^^^^^^^\n  File \"/app/shared/clients/time4vps.py\", line 55, in get_servers\n    resp.raise_for_status()\n  File \"/usr/local/lib/python3.12/site-packages/httpx/_models.py\", line 829, in raise_for_status\n    raise HTTPStatusError(message, request=request, response=self)\nhttpx.HTTPStatusError: Client error '401 Unauthorized' for url 'https://billing.time4vps.com/api/server'\nFor more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/401"}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.server_details_sync_interval "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.provisioning_stuck_timeout_seconds "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.provisioning_trigger_cooldown_seconds "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers "HTTP/1.1 307 Temporary Redirect"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers/ "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers "HTTP/1.1 307 Temporary Redirect"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers/ "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/incidents/active "HTTP/1.1 200 OK"
scheduler-1  | {"servers_discovered": 0, "servers_updated": 0, "servers_missing": 0, "details_updated": 0, "triggers_published": 0, "incidents_resolved": 0, "duration_sec": 0.24, "event": "server_sync_complete", "level": "info", "logger": "src.tasks.server_sync", "timestamp": "2026-07-15T13:11:36.483777", "service": "scheduler", "lineno": 107, "func_name": "sync_servers_worker"}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.server_sync_interval "HTTP/1.1 200 OK"
```

## deploy-worker logs (last 30)
```
deploy-worker-1  | {"service": "deploy-worker", "log_format": "json", "log_level": "INFO", "event": "logging_initialized", "level": "info", "logger": "shared.log_config.config", "timestamp": "2026-07-15T08:21:40.426936", "lineno": 90, "func_name": "setup_logging"}
deploy-worker-1  | {"event": "redis_connected", "level": "info", "logger": "shared.redis.client", "timestamp": "2026-07-15T08:21:40.427628", "service": "deploy-worker", "lineno": 95, "func_name": "connect"}
deploy-worker-1  | {"consumer": "deploy-worker-1", "event": "deploy-worker_started", "level": "info", "logger": "src.consumers._base", "timestamp": "2026-07-15T08:21:40.427799", "service": "deploy-worker", "lineno": 140, "func_name": "run_queue_worker"}
```

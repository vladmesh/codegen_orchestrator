# Debug: full-deploy
**Time**: 2026-07-15T14:44:08.667818+00:00

## Context
- project_id: `af62d2f2-5a59-4abc-88c7-b418cbd968ea`
- project_name: `live-test-3572fdd8`
- scaffold_status: `active`
- task_id: `task-2576d843`
- task_status: `done`
- story_status: `pr_review`
- final_app_status: `None`
- deployed_url: `None`
- engineering_elapsed: `25`

## CI failure evidence
- none captured

## scaffolder logs (last 30)
```
ice": "scaffolder", "func_name": "update_branch_protection", "lineno": 68}
scaffolder-1  | {"project_id": "af62d2f2-5a59-4abc-88c7-b418cbd968ea", "repository_id": "repo-d7a87128", "event": "branch_protection_set", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-15T14:36:12.114729", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 231}
scaffolder-1  | HTTP Request: GET https://api.github.com/orgs/project-factory-organization/installation "HTTP/1.1 200 OK"
scaffolder-1  | HTTP Request: PATCH https://api.github.com/repos/project-factory-organization/live-test-3572fdd8 "HTTP/1.1 200 OK"
scaffolder-1  | {"owner": "project-factory-organization", "repo": "live-test-3572fdd8", "event": "repo_auto_merge_enabled", "level": "info", "logger": "shared.clients.github._actions", "timestamp": "2026-07-15T14:36:12.868354", "project_id": "af62d2f2-5a59-4abc-88c7-b418cbd968ea", "service": "scaffolder", "func_name": "enable_repo_auto_merge", "lineno": 91}
scaffolder-1  | {"project_id": "af62d2f2-5a59-4abc-88c7-b418cbd968ea", "repository_id": "repo-d7a87128", "event": "repo_auto_merge_enabled", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-15T14:36:12.868515", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 237}
scaffolder-1  | HTTP Request: PATCH http://api:8000/api/projects/af62d2f2-5a59-4abc-88c7-b418cbd968ea "HTTP/1.1 200 OK"
scaffolder-1  | {"project_id": "af62d2f2-5a59-4abc-88c7-b418cbd968ea", "status": "active", "event": "project_status_updated", "level": "info", "logger": "src.clients.api", "timestamp": "2026-07-15T14:36:12.887587", "service": "scaffolder", "func_name": "update_project_status", "lineno": 71}
scaffolder-1  | {"project_id": "af62d2f2-5a59-4abc-88c7-b418cbd968ea", "repository_id": "repo-d7a87128", "event": "scaffold_job_success", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-15T14:36:12.888236", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 242}
```

## engineering-worker logs (last 30)
```
c7-b418cbd968ea", "lineno": 63, "func_name": "_update_task_status"}
engineering-worker-1  | {"planning_task_id": "task-2576d843", "new_status": "done", "event": "task_status_updated", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-15T14:36:35.583991", "correlation_id": "2bdf633a-b90f-4398-9926-3c410e9c5a26", "service": "engineering-worker", "request_id": "bf76b314-f8f0-4ee2-a73e-f42d1b2fe5ac", "task_id": "eng-e8f8be2f0f2c", "story_id": "story-a7e4001f", "project_id": "af62d2f2-5a59-4abc-88c7-b418cbd968ea", "lineno": 63, "func_name": "_update_task_status"}
engineering-worker-1  | {"task_id": "eng-e8f8be2f0f2c", "planning_task_id": "task-2576d843", "skip_deploy": true, "effective_skip_deploy": true, "event": "deploy_decision", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-15T14:36:35.602035", "correlation_id": "2bdf633a-b90f-4398-9926-3c410e9c5a26", "service": "engineering-worker", "request_id": "bf76b314-f8f0-4ee2-a73e-f42d1b2fe5ac", "story_id": "story-a7e4001f", "project_id": "af62d2f2-5a59-4abc-88c7-b418cbd968ea", "lineno": 311, "func_name": "handle_engineering_success"}
engineering-worker-1  | {"task_id": "eng-e8f8be2f0f2c", "project_id": "af62d2f2-5a59-4abc-88c7-b418cbd968ea", "event": "deploy_skipped", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-15T14:36:35.602584", "correlation_id": "2bdf633a-b90f-4398-9926-3c410e9c5a26", "service": "engineering-worker", "request_id": "bf76b314-f8f0-4ee2-a73e-f42d1b2fe5ac", "story_id": "story-a7e4001f", "lineno": 385, "func_name": "handle_engineering_success"}
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 1.14s.
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 1.86s.
engineering-worker-1  | Failed to export span batch due to timeout, max retries or shutdown.
```

## scheduler logs (last 30)
```
illing.time4vps.com/api/server/273978 "HTTP/1.1 401 Unauthorized"
scheduler-1  | {"server_handle": "vps-273978", "error": "Client error '401 Unauthorized' for url 'https://billing.time4vps.com/api/server/273978'\nFor more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/401", "error_type": "HTTPStatusError", "event": "server_details_fetch_failed", "level": "warning", "logger": "src.tasks.server_sync", "timestamp": "2026-07-15T14:44:08.497456", "service": "scheduler", "lineno": 300, "func_name": "_sync_server_details"}
scheduler-1  | {"updated_count": 0, "event": "server_details_sync_complete", "level": "info", "logger": "src.tasks.server_sync", "timestamp": "2026-07-15T14:44:08.497663", "service": "scheduler", "lineno": 308, "func_name": "_sync_server_details"}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.provisioning_stuck_timeout_seconds "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.provisioning_trigger_cooldown_seconds "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers "HTTP/1.1 307 Temporary Redirect"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers/ "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers "HTTP/1.1 307 Temporary Redirect"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers/ "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/incidents/active "HTTP/1.1 200 OK"
scheduler-1  | {"servers_discovered": 0, "servers_updated": 0, "servers_missing": 0, "details_updated": 0, "triggers_published": 0, "incidents_resolved": 0, "duration_sec": 0.41, "event": "server_sync_complete", "level": "info", "logger": "src.tasks.server_sync", "timestamp": "2026-07-15T14:44:08.560976", "service": "scheduler", "lineno": 107, "func_name": "sync_servers_worker"}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.server_sync_interval "HTTP/1.1 200 OK"
```

## deploy-worker logs (last 30)
```
stamp": "2026-07-15T14:43:18.936457", "story_id": "story-a7e4001f", "project_id": "af62d2f2-5a59-4abc-88c7-b418cbd968ea", "request_id": "d48db4d4-eb30-4db7-b857-e4e3b8aab032", "service": "deploy-worker", "task_id": "deploy-retry-b118c874", "correlation_id": "03a6c951-4985-4889-8cb9-a9e4492ba3e7", "func_name": "wait_for_workflow_completion", "lineno": 281}
deploy-worker-1  | {"workflow": "deploy.yml", "status": "in_progress", "elapsed_sec": 95, "event": "workflow_in_progress", "level": "info", "logger": "shared.clients.github._actions", "timestamp": "2026-07-15T14:43:34.835057", "story_id": "story-a7e4001f", "project_id": "af62d2f2-5a59-4abc-88c7-b418cbd968ea", "request_id": "d48db4d4-eb30-4db7-b857-e4e3b8aab032", "service": "deploy-worker", "task_id": "deploy-retry-b118c874", "correlation_id": "03a6c951-4985-4889-8cb9-a9e4492ba3e7", "func_name": "wait_for_workflow_completion", "lineno": 281}
deploy-worker-1  | {"workflow": "deploy.yml", "status": "in_progress", "elapsed_sec": 110, "event": "workflow_in_progress", "level": "info", "logger": "shared.clients.github._actions", "timestamp": "2026-07-15T14:43:50.653637", "story_id": "story-a7e4001f", "project_id": "af62d2f2-5a59-4abc-88c7-b418cbd968ea", "request_id": "d48db4d4-eb30-4db7-b857-e4e3b8aab032", "service": "deploy-worker", "task_id": "deploy-retry-b118c874", "correlation_id": "03a6c951-4985-4889-8cb9-a9e4492ba3e7", "func_name": "wait_for_workflow_completion", "lineno": 281}
deploy-worker-1  | {"workflow": "deploy.yml", "status": "in_progress", "elapsed_sec": 126, "event": "workflow_in_progress", "level": "info", "logger": "shared.clients.github._actions", "timestamp": "2026-07-15T14:44:06.481012", "story_id": "story-a7e4001f", "project_id": "af62d2f2-5a59-4abc-88c7-b418cbd968ea", "request_id": "d48db4d4-eb30-4db7-b857-e4e3b8aab032", "service": "deploy-worker", "task_id": "deploy-retry-b118c874", "correlation_id": "03a6c951-4985-4889-8cb9-a9e4492ba3e7", "func_name": "wait_for_workflow_completion", "lineno": 281}
```

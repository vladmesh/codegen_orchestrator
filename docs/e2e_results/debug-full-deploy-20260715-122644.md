# Debug: full-deploy
**Time**: 2026-07-15T12:26:44.081263+00:00

## Context
- project_id: `5fc6e649-938f-41c4-8b84-3f9692f30c54`
- project_name: `live-test-17f1f0d2`
- scaffold_status: `active`
- task_id: `task-e94f3c81`
- task_status: `done`
- story_status: `pr_review`
- final_app_status: `None`
- deployed_url: `None`
- engineering_elapsed: `20`

## CI failure evidence
- fix_task_id: `task-fb3c15fc`
  run_id: `29414799823`
  head_sha: `695dac5676be3ea1518d77acdde465e5be2a4d13`
  fingerprint: `fe5ea3d3eb0cbbec`
  failed_jobs: `[{"failed_steps": ["Run integration tests"], "name": "lint-and-test"}]`
- fix_task_id: `task-b6c1bb4a`
  run_id: `29414907767`
  head_sha: `6922709ada4d9fb0d261f57bbc5e15d84ae34399`
  fingerprint: `fe5ea3d3eb0cbbec`
  failed_jobs: `[{"failed_steps": ["Run integration tests"], "name": "lint-and-test"}]`

## scaffolder logs (last 30)
```
ice": "scaffolder", "func_name": "update_branch_protection", "lineno": 68}
scaffolder-1  | {"project_id": "5fc6e649-938f-41c4-8b84-3f9692f30c54", "repository_id": "repo-1cf30373", "event": "branch_protection_set", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-15T12:18:48.568450", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 231}
scaffolder-1  | HTTP Request: GET https://api.github.com/orgs/project-factory-organization/installation "HTTP/1.1 200 OK"
scaffolder-1  | HTTP Request: PATCH https://api.github.com/repos/project-factory-organization/live-test-17f1f0d2 "HTTP/1.1 200 OK"
scaffolder-1  | {"owner": "project-factory-organization", "repo": "live-test-17f1f0d2", "event": "repo_auto_merge_enabled", "level": "info", "logger": "shared.clients.github._actions", "timestamp": "2026-07-15T12:18:49.331320", "project_id": "5fc6e649-938f-41c4-8b84-3f9692f30c54", "service": "scaffolder", "func_name": "enable_repo_auto_merge", "lineno": 91}
scaffolder-1  | {"project_id": "5fc6e649-938f-41c4-8b84-3f9692f30c54", "repository_id": "repo-1cf30373", "event": "repo_auto_merge_enabled", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-15T12:18:49.331512", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 237}
scaffolder-1  | HTTP Request: PATCH http://api:8000/api/projects/5fc6e649-938f-41c4-8b84-3f9692f30c54 "HTTP/1.1 200 OK"
scaffolder-1  | {"project_id": "5fc6e649-938f-41c4-8b84-3f9692f30c54", "status": "active", "event": "project_status_updated", "level": "info", "logger": "src.clients.api", "timestamp": "2026-07-15T12:18:49.345511", "service": "scaffolder", "func_name": "update_project_status", "lineno": 71}
scaffolder-1  | {"project_id": "5fc6e649-938f-41c4-8b84-3f9692f30c54", "repository_id": "repo-1cf30373", "event": "scaffold_job_success", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-15T12:18:49.345673", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 242}
```

## engineering-worker logs (last 30)
```
84-3f9692f30c54", "lineno": 63, "func_name": "_update_task_status"}
engineering-worker-1  | {"planning_task_id": "task-b6c1bb4a", "new_status": "done", "event": "task_status_updated", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-15T12:23:35.366973", "correlation_id": "3ba0ec3e-e19b-483e-947e-25b19f4e504e", "service": "engineering-worker", "request_id": "05940899-f74f-4830-b264-324c9588c1cd", "task_id": "eng-09b6535c51a8", "story_id": "story-7e5fca2e", "project_id": "5fc6e649-938f-41c4-8b84-3f9692f30c54", "lineno": 63, "func_name": "_update_task_status"}
engineering-worker-1  | {"task_id": "eng-09b6535c51a8", "planning_task_id": "task-b6c1bb4a", "skip_deploy": true, "effective_skip_deploy": true, "event": "deploy_decision", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-15T12:23:35.375013", "correlation_id": "3ba0ec3e-e19b-483e-947e-25b19f4e504e", "service": "engineering-worker", "request_id": "05940899-f74f-4830-b264-324c9588c1cd", "story_id": "story-7e5fca2e", "project_id": "5fc6e649-938f-41c4-8b84-3f9692f30c54", "lineno": 311, "func_name": "handle_engineering_success"}
engineering-worker-1  | {"task_id": "eng-09b6535c51a8", "project_id": "5fc6e649-938f-41c4-8b84-3f9692f30c54", "event": "deploy_skipped", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-15T12:23:35.375174", "correlation_id": "3ba0ec3e-e19b-483e-947e-25b19f4e504e", "service": "engineering-worker", "request_id": "05940899-f74f-4830-b264-324c9588c1cd", "story_id": "story-7e5fca2e", "lineno": 385, "func_name": "handle_engineering_success"}
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 1.17s.
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 1.97s.
engineering-worker-1  | Failed to export span batch due to timeout, max retries or shutdown.
```

## scheduler logs (last 30)
```
0, "servers_missing": 0, "details_updated": 0, "triggers_published": 0, "incidents_resolved": 0, "duration_sec": 0.25, "event": "server_sync_complete", "level": "info", "logger": "src.tasks.server_sync", "timestamp": "2026-07-15T12:26:38.462041", "service": "scheduler", "lineno": 107, "func_name": "sync_servers_worker"}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.server_sync_interval "HTTP/1.1 200 OK"
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
scheduler-1  | {"tasks_dispatched": 0, "stories_completed": 0, "scaffolds_triggered": 0, "prs_merged": 0, "event": "dispatcher_cycle", "level": "info", "logger": "src.tasks.task_dispatcher", "timestamp": "2026-07-15T12:26:41.471781", "service": "scheduler", "lineno": 234, "func_name": "task_dispatcher_loop"}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.dispatch_interval_seconds "HTTP/1.1 200 OK"
```

## deploy-worker logs (last 30)
```
deploy-worker-1  | {"service": "deploy-worker", "log_format": "json", "log_level": "INFO", "event": "logging_initialized", "level": "info", "logger": "shared.log_config.config", "timestamp": "2026-07-15T08:21:40.426936", "lineno": 90, "func_name": "setup_logging"}
deploy-worker-1  | {"event": "redis_connected", "level": "info", "logger": "shared.redis.client", "timestamp": "2026-07-15T08:21:40.427628", "service": "deploy-worker", "lineno": 95, "func_name": "connect"}
deploy-worker-1  | {"consumer": "deploy-worker-1", "event": "deploy-worker_started", "level": "info", "logger": "src.consumers._base", "timestamp": "2026-07-15T08:21:40.427799", "service": "deploy-worker", "lineno": 140, "func_name": "run_queue_worker"}
```

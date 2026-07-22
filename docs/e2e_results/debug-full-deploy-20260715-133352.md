# Debug: full-deploy
**Time**: 2026-07-15T13:33:52.689501+00:00

## Context
- project_id: `706afc66-ed47-4564-b4bc-696b6fb380bf`
- project_name: `live-test-a111dc2b`
- scaffold_status: `active`
- task_id: `task-e501a088`
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
scaffolder-1  | {"project_id": "706afc66-ed47-4564-b4bc-696b6fb380bf", "repository_id": "repo-abd1c7fd", "event": "branch_protection_set", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-15T13:25:59.524082", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 231}
scaffolder-1  | HTTP Request: GET https://api.github.com/orgs/project-factory-organization/installation "HTTP/1.1 200 OK"
scaffolder-1  | HTTP Request: PATCH https://api.github.com/repos/project-factory-organization/live-test-a111dc2b "HTTP/1.1 200 OK"
scaffolder-1  | {"owner": "project-factory-organization", "repo": "live-test-a111dc2b", "event": "repo_auto_merge_enabled", "level": "info", "logger": "shared.clients.github._actions", "timestamp": "2026-07-15T13:26:00.348058", "project_id": "706afc66-ed47-4564-b4bc-696b6fb380bf", "service": "scaffolder", "func_name": "enable_repo_auto_merge", "lineno": 91}
scaffolder-1  | {"project_id": "706afc66-ed47-4564-b4bc-696b6fb380bf", "repository_id": "repo-abd1c7fd", "event": "repo_auto_merge_enabled", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-15T13:26:00.348222", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 237}
scaffolder-1  | HTTP Request: PATCH http://api:8000/api/projects/706afc66-ed47-4564-b4bc-696b6fb380bf "HTTP/1.1 200 OK"
scaffolder-1  | {"project_id": "706afc66-ed47-4564-b4bc-696b6fb380bf", "status": "active", "event": "project_status_updated", "level": "info", "logger": "src.clients.api", "timestamp": "2026-07-15T13:26:00.366310", "service": "scaffolder", "func_name": "update_project_status", "lineno": 71}
scaffolder-1  | {"project_id": "706afc66-ed47-4564-b4bc-696b6fb380bf", "repository_id": "repo-abd1c7fd", "event": "scaffold_job_success", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-15T13:26:00.366471", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 242}
```

## engineering-worker logs (last 30)
```
bc-696b6fb380bf", "lineno": 63, "func_name": "_update_task_status"}
engineering-worker-1  | {"planning_task_id": "task-e501a088", "new_status": "done", "event": "task_status_updated", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-15T13:26:22.517639", "correlation_id": "6c4d4131-bde8-4bbe-9ee3-37bfe130afec", "service": "engineering-worker", "request_id": "2ff69293-aff5-4be4-9058-cd50b9f58525", "task_id": "eng-82e3b67db1cd", "story_id": "story-d14380a3", "project_id": "706afc66-ed47-4564-b4bc-696b6fb380bf", "lineno": 63, "func_name": "_update_task_status"}
engineering-worker-1  | {"task_id": "eng-82e3b67db1cd", "planning_task_id": "task-e501a088", "skip_deploy": true, "effective_skip_deploy": true, "event": "deploy_decision", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-15T13:26:22.532561", "correlation_id": "6c4d4131-bde8-4bbe-9ee3-37bfe130afec", "service": "engineering-worker", "request_id": "2ff69293-aff5-4be4-9058-cd50b9f58525", "story_id": "story-d14380a3", "project_id": "706afc66-ed47-4564-b4bc-696b6fb380bf", "lineno": 311, "func_name": "handle_engineering_success"}
engineering-worker-1  | {"task_id": "eng-82e3b67db1cd", "project_id": "706afc66-ed47-4564-b4bc-696b6fb380bf", "event": "deploy_skipped", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-15T13:26:22.532849", "correlation_id": "6c4d4131-bde8-4bbe-9ee3-37bfe130afec", "service": "engineering-worker", "request_id": "2ff69293-aff5-4be4-9058-cd50b9f58525", "story_id": "story-d14380a3", "lineno": 385, "func_name": "handle_engineering_success"}
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 0.83s.
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 2.21s.
engineering-worker-1  | Failed to export span batch due to timeout, max retries or shutdown.
```

## scheduler logs (last 30)
```
http://api:8000/api/repositories/by-provider-id/1301645873 "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/projects/706afc66-ed47-4564-b4bc-696b6fb380bf "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET https://api.github.com/repos/project-factory-organization/live-test-a111dc2b/installation "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET https://api.github.com/repos/project-factory-organization/live-test-a111dc2b/contents/.project-spec.yaml?ref=main "HTTP/1.1 404 Not Found"
scheduler-1  | HTTP Request: GET https://api.github.com/repos/project-factory-organization/live-test-a111dc2b/installation "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET https://api.github.com/repos/project-factory-organization/live-test-a111dc2b/contents/README.md?ref=main "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: POST http://api:8000/api/rag/ingest "HTTP/1.1 500 Internal Server Error"
scheduler-1  | {"project_id": "706afc66-ed47-4564-b4bc-696b6fb380bf", "repo": "project-factory-organization/live-test-a111dc2b", "status_code": 500, "detail": "{\"detail\":\"Failed to ingest documents\"}", "event": "rag_ingest_http_error", "level": "warning", "logger": "src.tasks.github_sync", "timestamp": "2026-07-15T13:33:45.767497", "service": "scheduler", "lineno": 93, "func_name": "_ingest_to_rag"}
scheduler-1  | HTTP Request: GET http://api:8000/api/projects "HTTP/1.1 307 Temporary Redirect"
scheduler-1  | HTTP Request: GET http://api:8000/api/projects/ "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/repositories/?project_id=706afc66-ed47-4564-b4bc-696b6fb380bf "HTTP/1.1 200 OK"
scheduler-1  | {"repos_synced": 4, "duration_sec": 2.43, "event": "github_sync_complete", "level": "info", "logger": "src.tasks.github_sync", "timestamp": "2026-07-15T13:33:45.784947", "service": "scheduler", "lineno": 394, "func_name": "sync_projects_worker"}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.github_sync_interval "HTTP/1.1 200 OK"
```

## deploy-worker logs (last 30)
```
NTIME_CONTEXT.detach(token)
deploy-worker-1  |   File "/usr/local/lib/python3.12/site-packages/opentelemetry/context/contextvars_context.py", line 53, in detach
deploy-worker-1  |     self._current_context.reset(token)
deploy-worker-1  | ValueError: <Token var=<ContextVar name='current_context' default={} at 0x7b95944db2e0> at 0x7b958c107f00> was created in a different Context
deploy-worker-1  | {"task_id": "deploy-poll-0ba7c203", "result_keys": ["allocated_resources", "deployed_url", "deployment_result", "env_analysis", "env_variables", "errors", "head_sha", "messages", "missing_user_secrets", "project_id", "project_spec", "provided_secrets", "repo_info", "resolved_secrets", "smoke_result"], "has_smoke_result": true, "smoke_result": null, "deployed_url": null, "errors": [], "event": "devops_subgraph_result", "level": "info", "logger": "__main__", "timestamp": "2026-07-15T13:28:38.980670", "story_id": "story-d14380a3", "service": "deploy-worker", "project_id": "706afc66-ed47-4564-b4bc-696b6fb380bf", "request_id": "76742cfc-7bc8-4333-85bc-1cd4be7d044e", "correlation_id": "1cb23156-f275-46fa-a797-e184d7c55b4f", "lineno": 347, "func_name": "process_deploy_job"}
deploy-worker-1  | {"task_id": "deploy-poll-0ba7c203", "missing": ["POSTGRES_HOST_PORT", "REDIS_HOST_PORT"], "event": "deploy_job_missing_secrets", "level": "info", "logger": "__main__", "timestamp": "2026-07-15T13:28:38.980965", "story_id": "story-d14380a3", "service": "deploy-worker", "project_id": "706afc66-ed47-4564-b4bc-696b6fb380bf", "request_id": "76742cfc-7bc8-4333-85bc-1cd4be7d044e", "correlation_id": "1cb23156-f275-46fa-a797-e184d7c55b4f", "lineno": 390, "func_name": "process_deploy_job"}
deploy-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 0.93s.
deploy-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 2.29s.
deploy-worker-1  | Failed to export span batch due to timeout, max retries or shutdown.
```

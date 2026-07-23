# Debug: full-llm-deploy-run
**Time**: 2026-07-23T19:52:52.356889+00:00

## Context
- project_id: `f809755d-d786-465c-a402-f8147c0f3755`
- project_name: `live-te-f809755dd786465ca402f8147c0f3755`
- scaffold_status: `active`
- task_id: `task-9ba7d1ee`
- task_status: `done`
- story_status: `in_progress`
- final_app_status: `None`
- deployed_url: `None`
- engineering_elapsed: `295`
- deploy_run_id: `None`
- deploy_head_sha: `None`
- deploy_run_status: `None`
- deploy_outcome: `None`
- deploy_error_details: `None`
- deploy_run_error: `no deploy run with a merged head_sha appeared for story story-fe15e15c within 420s`
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
scaffolder-1  | {"project_id": "f809755d-d786-465c-a402-f8147c0f3755", "repository_id": "repo-f055b9a4", "event": "branch_protection_set", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-23T19:39:49.835888", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 231}
scaffolder-1  | HTTP Request: GET https://api.github.com/orgs/project-factory-organization/installation "HTTP/1.1 200 OK"
scaffolder-1  | HTTP Request: PATCH https://api.github.com/repos/project-factory-organization/live-te-f809755dd786465ca402f8147c0f3755 "HTTP/1.1 200 OK"
scaffolder-1  | {"owner": "project-factory-organization", "repo": "live-te-f809755dd786465ca402f8147c0f3755", "event": "repo_auto_merge_enabled", "level": "info", "logger": "shared.clients.github._actions", "timestamp": "2026-07-23T19:39:50.574443", "project_id": "f809755d-d786-465c-a402-f8147c0f3755", "service": "scaffolder", "func_name": "enable_repo_auto_merge", "lineno": 96}
scaffolder-1  | {"project_id": "f809755d-d786-465c-a402-f8147c0f3755", "repository_id": "repo-f055b9a4", "event": "repo_auto_merge_enabled", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-23T19:39:50.574876", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 237}
scaffolder-1  | HTTP Request: PATCH http://api:8000/api/projects/f809755d-d786-465c-a402-f8147c0f3755 "HTTP/1.1 200 OK"
scaffolder-1  | {"project_id": "f809755d-d786-465c-a402-f8147c0f3755", "status": "active", "event": "project_status_updated", "level": "info", "logger": "src.clients.api", "timestamp": "2026-07-23T19:39:50.596065", "service": "scaffolder", "func_name": "update_project_status", "lineno": 71}
scaffolder-1  | {"project_id": "f809755d-d786-465c-a402-f8147c0f3755", "repository_id": "repo-f055b9a4", "event": "scaffold_job_success", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-23T19:39:50.596327", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 242}
```

## engineering-worker logs (last 30)
```
67-9da504ca303a", "lineno": 64, "func_name": "_update_task_status"}
engineering-worker-1  | {"planning_task_id": "task-9ba7d1ee", "new_status": "done", "event": "task_status_updated", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-23T19:44:47.212218", "task_id": "eng-1c6756c5bcd8", "story_id": "story-fe15e15c", "project_id": "f809755d-d786-465c-a402-f8147c0f3755", "service": "engineering-worker", "correlation_id": "d5892824-b07d-4bbd-8675-49d007f2d547", "request_id": "d46abd0e-5db9-4480-9f67-9da504ca303a", "lineno": 64, "func_name": "_update_task_status"}
engineering-worker-1  | {"task_id": "eng-1c6756c5bcd8", "planning_task_id": "task-9ba7d1ee", "skip_deploy": true, "effective_skip_deploy": true, "event": "deploy_decision", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-23T19:44:47.224260", "story_id": "story-fe15e15c", "project_id": "f809755d-d786-465c-a402-f8147c0f3755", "service": "engineering-worker", "correlation_id": "d5892824-b07d-4bbd-8675-49d007f2d547", "request_id": "d46abd0e-5db9-4480-9f67-9da504ca303a", "lineno": 316, "func_name": "handle_engineering_success"}
engineering-worker-1  | {"task_id": "eng-1c6756c5bcd8", "project_id": "f809755d-d786-465c-a402-f8147c0f3755", "event": "deploy_skipped", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-23T19:44:47.224619", "story_id": "story-fe15e15c", "service": "engineering-worker", "correlation_id": "d5892824-b07d-4bbd-8675-49d007f2d547", "request_id": "d46abd0e-5db9-4480-9f67-9da504ca303a", "lineno": 391, "func_name": "handle_engineering_success"}
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 1.10s.
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 1.76s.
engineering-worker-1  | Failed to export span batch due to timeout, max retries or shutdown.
```

## scheduler logs (last 30)
```
       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
scheduler-1  |   File "/app/shared/clients/github/_pull_requests.py", line 55, in create_pull_request
scheduler-1  |     raise RuntimeError(
scheduler-1  | RuntimeError: PR creation returned 422 but no existing PR found for story/story-fe15e15c->main
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=pr_review "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=pr_review "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=created "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=in_progress "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/tasks/?status=in_dev "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/tasks/?status=failed "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=deploying "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=waiting_user_secret "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=testing "HTTP/1.1 200 OK"
scheduler-1  | {"tasks_dispatched": 0, "stories_completed": 0, "scaffolds_triggered": 0, "prs_merged": 0, "event": "dispatcher_cycle", "level": "info", "logger": "src.tasks.task_dispatcher", "timestamp": "2026-07-23T19:52:29.052145", "service": "scheduler", "func_name": "task_dispatcher_loop", "lineno": 369}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.dispatch_interval_seconds "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers "HTTP/1.1 307 Temporary Redirect"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers/ "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/health.http_timeout "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.rag_summarizer_poll_interval "HTTP/1.1 200 OK"
```

## deploy-worker logs (last 30)
```
elf._current_context.reset(token)
deploy-worker-1  | ValueError: <Token var=<ContextVar name='current_context' default={} at 0x7b8fdf12a890> at 0x7b8fccb55c00> was created in a different Context
deploy-worker-1  | {"task_id": "deploy-poll-ee092bd5", "result_keys": ["allocated_resources", "application_id", "deployed_url", "deployment_result", "environment_contract", "errors", "head_sha", "messages", "missing_user_secrets", "non_secret_values", "project_id", "project_spec", "provided_secrets", "repo_info", "resolution_outcome", "run_id", "secret_values", "smoke_result"], "has_smoke_result": true, "smoke_result": {"status": "pass", "checks": [{"module": "backend", "result": "pass", "detail": "HTTP 200"}]}, "deployed_url": "http://185.81.166.84:8000", "errors": [], "event": "devops_subgraph_result", "level": "info", "logger": "__main__", "timestamp": "2026-07-23T19:39:05.052018", "correlation_id": "00069814-8b5d-4fef-bedd-e4dd14a158b1", "request_id": "271ee10b-d11a-429f-a1af-17c3f7acc2bb", "story_id": "story-bee84d59", "project_id": "c3a56729-4f9f-4f13-b064-eb01795e66c3", "service": "deploy-worker", "func_name": "process_deploy_job", "lineno": 374}
deploy-worker-1  | {"task_id": "deploy-poll-ee092bd5", "deployed_url": "http://185.81.166.84:8000", "event": "deploy_job_success", "level": "info", "logger": "src.consumers.deploy_result_handler", "timestamp": "2026-07-23T19:39:05.052774", "correlation_id": "00069814-8b5d-4fef-bedd-e4dd14a158b1", "request_id": "271ee10b-d11a-429f-a1af-17c3f7acc2bb", "story_id": "story-bee84d59", "project_id": "c3a56729-4f9f-4f13-b064-eb01795e66c3", "service": "deploy-worker", "func_name": "_handle_deploy_success", "lineno": 111}
deploy-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 1.09s.
deploy-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 1.66s.
deploy-worker-1  | Failed to export span batch due to timeout, max retries or shutdown.
```

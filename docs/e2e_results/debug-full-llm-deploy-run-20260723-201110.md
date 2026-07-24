# Debug: full-llm-deploy-run
**Time**: 2026-07-23T20:11:10.298542+00:00

## Context
- project_id: `56af03ea-964e-4519-868e-5537bc529192`
- project_name: `live-te-56af03ea964e4519868e5537bc529192`
- scaffold_status: `active`
- task_id: `task-8b5c21da`
- task_status: `done`
- story_status: `in_progress`
- final_app_status: `None`
- deployed_url: `None`
- engineering_elapsed: `300`
- deploy_run_id: `None`
- deploy_head_sha: `None`
- deploy_run_status: `None`
- deploy_outcome: `None`
- deploy_error_details: `None`
- deploy_run_error: `no deploy run with a merged head_sha appeared for story story-df6c5e88 within 420s`
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
scaffolder-1  | {"project_id": "56af03ea-964e-4519-868e-5537bc529192", "repository_id": "repo-e53ba078", "event": "branch_protection_set", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-23T19:58:02.088743", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 231}
scaffolder-1  | HTTP Request: GET https://api.github.com/orgs/project-factory-organization/installation "HTTP/1.1 200 OK"
scaffolder-1  | HTTP Request: PATCH https://api.github.com/repos/project-factory-organization/live-te-56af03ea964e4519868e5537bc529192 "HTTP/1.1 200 OK"
scaffolder-1  | {"owner": "project-factory-organization", "repo": "live-te-56af03ea964e4519868e5537bc529192", "event": "repo_auto_merge_enabled", "level": "info", "logger": "shared.clients.github._actions", "timestamp": "2026-07-23T19:58:02.785168", "project_id": "56af03ea-964e-4519-868e-5537bc529192", "service": "scaffolder", "func_name": "enable_repo_auto_merge", "lineno": 96}
scaffolder-1  | {"project_id": "56af03ea-964e-4519-868e-5537bc529192", "repository_id": "repo-e53ba078", "event": "repo_auto_merge_enabled", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-23T19:58:02.785343", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 237}
scaffolder-1  | HTTP Request: PATCH http://api:8000/api/projects/56af03ea-964e-4519-868e-5537bc529192 "HTTP/1.1 200 OK"
scaffolder-1  | {"project_id": "56af03ea-964e-4519-868e-5537bc529192", "status": "active", "event": "project_status_updated", "level": "info", "logger": "src.clients.api", "timestamp": "2026-07-23T19:58:02.799704", "service": "scaffolder", "func_name": "update_project_status", "lineno": 71}
scaffolder-1  | {"project_id": "56af03ea-964e-4519-868e-5537bc529192", "repository_id": "repo-e53ba078", "event": "scaffold_job_success", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-23T19:58:02.799936", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 242}
```

## engineering-worker logs (last 30)
```
68-265cb443b2a6", "lineno": 64, "func_name": "_update_task_status"}
engineering-worker-1  | {"planning_task_id": "task-8b5c21da", "new_status": "done", "event": "task_status_updated", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-23T20:03:04.642182", "task_id": "eng-45864f4b911a", "story_id": "story-df6c5e88", "project_id": "56af03ea-964e-4519-868e-5537bc529192", "service": "engineering-worker", "correlation_id": "eb975e97-d1b9-4de1-b178-c33e4212e235", "request_id": "4dbab91a-4624-48c0-8868-265cb443b2a6", "lineno": 64, "func_name": "_update_task_status"}
engineering-worker-1  | {"task_id": "eng-45864f4b911a", "planning_task_id": "task-8b5c21da", "skip_deploy": true, "effective_skip_deploy": true, "event": "deploy_decision", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-23T20:03:04.655555", "story_id": "story-df6c5e88", "project_id": "56af03ea-964e-4519-868e-5537bc529192", "service": "engineering-worker", "correlation_id": "eb975e97-d1b9-4de1-b178-c33e4212e235", "request_id": "4dbab91a-4624-48c0-8868-265cb443b2a6", "lineno": 316, "func_name": "handle_engineering_success"}
engineering-worker-1  | {"task_id": "eng-45864f4b911a", "project_id": "56af03ea-964e-4519-868e-5537bc529192", "event": "deploy_skipped", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-23T20:03:04.656300", "story_id": "story-df6c5e88", "service": "engineering-worker", "correlation_id": "eb975e97-d1b9-4de1-b178-c33e4212e235", "request_id": "4dbab91a-4624-48c0-8868-265cb443b2a6", "lineno": 391, "func_name": "handle_engineering_success"}
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 0.98s.
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 1.76s.
engineering-worker-1  | Failed to export span batch due to timeout, max retries or shutdown.
```

## scheduler logs (last 30)
```
mation check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/422
scheduler-1  | 
scheduler-1  | The above exception was the direct cause of the following exception:
scheduler-1  | 
scheduler-1  | Traceback (most recent call last):
scheduler-1  |   File "/app/src/tasks/story_completion.py", line 161, in complete_stories
scheduler-1  |     pr = await github.create_pull_request(
scheduler-1  |          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
scheduler-1  |   File "/app/shared/clients/github/_pull_requests.py", line 55, in create_pull_request
scheduler-1  |     raise RuntimeError(
scheduler-1  | RuntimeError: PR creation returned 422 but no existing PR found for story/story-df6c5e88->main
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=pr_review "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=pr_review "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=created "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=in_progress "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/tasks/?status=in_dev "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/tasks/?status=failed "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=deploying "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=waiting_user_secret "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=testing "HTTP/1.1 200 OK"
scheduler-1  | {"tasks_dispatched": 0, "stories_completed": 0, "scaffolds_triggered": 0, "prs_merged": 0, "event": "dispatcher_cycle", "level": "info", "logger": "src.tasks.task_dispatcher", "timestamp": "2026-07-23T20:11:07.808240", "service": "scheduler", "func_name": "task_dispatcher_loop", "lineno": 369}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.dispatch_interval_seconds "HTTP/1.1 200 OK"
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

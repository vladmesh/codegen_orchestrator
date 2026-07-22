# Debug: full-deploy-run
**Time**: 2026-07-16T20:43:43.775956+00:00

## Context
- project_id: `f7e12df3-60be-4580-b398-c9a2d2cf2f5e`
- project_name: `live-test-d46535f8`
- scaffold_status: `active`
- task_id: `task-cc0f3abd`
- task_status: `done`
- story_status: `pr_review`
- final_app_status: `None`
- deployed_url: `None`
- engineering_elapsed: `70`
- deploy_run_id: `None`
- deploy_head_sha: `None`
- deploy_run_status: `None`
- deploy_outcome: `None`
- deploy_error_details: `None`
- deploy_run_error: `no deploy run with a merged head_sha appeared for project f7e12df3-60be-4580-b398-c9a2d2cf2f5e within 420s`
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
ice": "scaffolder", "lineno": 73, "func_name": "update_branch_protection"}
scaffolder-1  | {"project_id": "f7e12df3-60be-4580-b398-c9a2d2cf2f5e", "repository_id": "repo-181548a0", "event": "branch_protection_set", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-16T20:35:10.949375", "service": "scaffolder", "lineno": 231, "func_name": "_process_full_mode"}
scaffolder-1  | HTTP Request: GET https://api.github.com/orgs/project-factory-organization/installation "HTTP/1.1 200 OK"
scaffolder-1  | HTTP Request: PATCH https://api.github.com/repos/project-factory-organization/live-test-d46535f8 "HTTP/1.1 200 OK"
scaffolder-1  | {"owner": "project-factory-organization", "repo": "live-test-d46535f8", "event": "repo_auto_merge_enabled", "level": "info", "logger": "shared.clients.github._actions", "timestamp": "2026-07-16T20:35:11.661974", "project_id": "f7e12df3-60be-4580-b398-c9a2d2cf2f5e", "service": "scaffolder", "lineno": 96, "func_name": "enable_repo_auto_merge"}
scaffolder-1  | {"project_id": "f7e12df3-60be-4580-b398-c9a2d2cf2f5e", "repository_id": "repo-181548a0", "event": "repo_auto_merge_enabled", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-16T20:35:11.662126", "service": "scaffolder", "lineno": 237, "func_name": "_process_full_mode"}
scaffolder-1  | HTTP Request: PATCH http://api:8000/api/projects/f7e12df3-60be-4580-b398-c9a2d2cf2f5e "HTTP/1.1 200 OK"
scaffolder-1  | {"project_id": "f7e12df3-60be-4580-b398-c9a2d2cf2f5e", "status": "active", "event": "project_status_updated", "level": "info", "logger": "src.clients.api", "timestamp": "2026-07-16T20:35:11.748044", "service": "scaffolder", "lineno": 71, "func_name": "update_project_status"}
scaffolder-1  | {"project_id": "f7e12df3-60be-4580-b398-c9a2d2cf2f5e", "repository_id": "repo-181548a0", "event": "scaffold_job_success", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-16T20:35:11.748267", "service": "scaffolder", "lineno": 242, "func_name": "_process_full_mode"}
```

## engineering-worker logs (last 30)
```
48-a3a4afde6122", "func_name": "_update_task_status", "lineno": 63}
engineering-worker-1  | {"planning_task_id": "task-cc0f3abd", "new_status": "done", "event": "task_status_updated", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-16T20:36:25.564695", "task_id": "eng-22d2680a1bc9", "request_id": "e4493258-428f-4239-a841-e155844cf087", "story_id": "story-3de09248", "project_id": "f7e12df3-60be-4580-b398-c9a2d2cf2f5e", "service": "engineering-worker", "correlation_id": "25a9f5ce-e528-4d09-bd48-a3a4afde6122", "func_name": "_update_task_status", "lineno": 63}
engineering-worker-1  | {"task_id": "eng-22d2680a1bc9", "planning_task_id": "task-cc0f3abd", "skip_deploy": true, "effective_skip_deploy": true, "event": "deploy_decision", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-16T20:36:25.585008", "request_id": "e4493258-428f-4239-a841-e155844cf087", "story_id": "story-3de09248", "project_id": "f7e12df3-60be-4580-b398-c9a2d2cf2f5e", "service": "engineering-worker", "correlation_id": "25a9f5ce-e528-4d09-bd48-a3a4afde6122", "func_name": "handle_engineering_success", "lineno": 311}
engineering-worker-1  | {"task_id": "eng-22d2680a1bc9", "project_id": "f7e12df3-60be-4580-b398-c9a2d2cf2f5e", "event": "deploy_skipped", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-16T20:36:25.585241", "request_id": "e4493258-428f-4239-a841-e155844cf087", "story_id": "story-3de09248", "service": "engineering-worker", "correlation_id": "25a9f5ce-e528-4d09-bd48-a3a4afde6122", "func_name": "handle_engineering_success", "lineno": 385}
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 0.86s.
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 2.34s.
engineering-worker-1  | Failed to export span batch due to timeout, max retries or shutdown.
```

## scheduler logs (last 30)
```
_sync_interval "HTTP/1.1 200 OK"
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
scheduler-1  | {"tasks_dispatched": 0, "stories_completed": 0, "scaffolds_triggered": 0, "prs_merged": 0, "event": "dispatcher_cycle", "level": "info", "logger": "src.tasks.task_dispatcher", "timestamp": "2026-07-16T20:43:22.274503", "service": "scheduler", "func_name": "task_dispatcher_loop", "lineno": 234}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.dispatch_interval_seconds "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.rag_summarizer_poll_interval "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers "HTTP/1.1 307 Temporary Redirect"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers/ "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/health.http_timeout "HTTP/1.1 200 OK"
```

## deploy-worker logs (last 30)
```
elf._current_context.reset(token)
deploy-worker-1  | ValueError: <Token var=<ContextVar name='current_context' default={} at 0x7130d2de2e80> at 0x7130c082c340> was created in a different Context
deploy-worker-1  | {"task_id": "deploy-poll-ea0bed35", "result_keys": ["allocated_resources", "application_id", "deployed_url", "deployment_result", "environment_contract", "errors", "head_sha", "messages", "missing_user_secrets", "non_secret_values", "project_id", "project_spec", "provided_secrets", "repo_info", "resolution_outcome", "run_id", "secret_values", "smoke_result"], "has_smoke_result": true, "smoke_result": {"status": "pass", "checks": [{"module": "backend", "result": "pass", "detail": "HTTP 200"}]}, "deployed_url": "http://185.81.166.84:8000", "errors": [], "event": "devops_subgraph_result", "level": "info", "logger": "__main__", "timestamp": "2026-07-16T20:39:57.345066", "correlation_id": "4d318742-7803-4bc8-ac13-41f76d55e65a", "request_id": "8196e7fe-08db-4664-adb9-5cca313e9efe", "story_id": "story-3de09248", "service": "deploy-worker", "project_id": "f7e12df3-60be-4580-b398-c9a2d2cf2f5e", "func_name": "process_deploy_job", "lineno": 336}
deploy-worker-1  | {"task_id": "deploy-poll-ea0bed35", "deployed_url": "http://185.81.166.84:8000", "event": "deploy_job_success", "level": "info", "logger": "src.consumers.deploy_result_handler", "timestamp": "2026-07-16T20:39:57.345321", "correlation_id": "4d318742-7803-4bc8-ac13-41f76d55e65a", "request_id": "8196e7fe-08db-4664-adb9-5cca313e9efe", "story_id": "story-3de09248", "service": "deploy-worker", "project_id": "f7e12df3-60be-4580-b398-c9a2d2cf2f5e", "func_name": "_handle_deploy_success", "lineno": 108}
deploy-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 0.92s.
deploy-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 1.75s.
deploy-worker-1  | Failed to export span batch due to timeout, max retries or shutdown.
```

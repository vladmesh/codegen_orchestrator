# Debug: full-llm-deploy-run
**Time**: 2026-07-23T02:08:14.101485+00:00

## Context
- project_id: `44045950-40d3-4d94-a5a2-3e99cdd79c8c`
- project_name: `live-te-4404595040d34d94a5a23e99cdd79c8c`
- scaffold_status: `active`
- task_id: `task-ca3f7b26`
- task_status: `done`
- story_status: `in_progress`
- final_app_status: `None`
- deployed_url: `None`
- engineering_elapsed: `175`
- deploy_run_id: `None`
- deploy_head_sha: `None`
- deploy_run_status: `None`
- deploy_outcome: `None`
- deploy_error_details: `None`
- deploy_run_error: `no deploy run with a merged head_sha appeared for story story-aa9e5cfe within 420s`
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
scaffolder-1  | {"project_id": "44045950-40d3-4d94-a5a2-3e99cdd79c8c", "repository_id": "repo-bcdad454", "event": "branch_protection_set", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-23T01:57:10.366862", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 231}
scaffolder-1  | HTTP Request: GET https://api.github.com/orgs/project-factory-organization/installation "HTTP/1.1 200 OK"
scaffolder-1  | HTTP Request: PATCH https://api.github.com/repos/project-factory-organization/live-te-4404595040d34d94a5a23e99cdd79c8c "HTTP/1.1 200 OK"
scaffolder-1  | {"owner": "project-factory-organization", "repo": "live-te-4404595040d34d94a5a23e99cdd79c8c", "event": "repo_auto_merge_enabled", "level": "info", "logger": "shared.clients.github._actions", "timestamp": "2026-07-23T01:57:11.250934", "project_id": "44045950-40d3-4d94-a5a2-3e99cdd79c8c", "service": "scaffolder", "func_name": "enable_repo_auto_merge", "lineno": 96}
scaffolder-1  | {"project_id": "44045950-40d3-4d94-a5a2-3e99cdd79c8c", "repository_id": "repo-bcdad454", "event": "repo_auto_merge_enabled", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-23T01:57:11.251141", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 237}
scaffolder-1  | HTTP Request: PATCH http://api:8000/api/projects/44045950-40d3-4d94-a5a2-3e99cdd79c8c "HTTP/1.1 200 OK"
scaffolder-1  | {"project_id": "44045950-40d3-4d94-a5a2-3e99cdd79c8c", "status": "active", "event": "project_status_updated", "level": "info", "logger": "src.clients.api", "timestamp": "2026-07-23T01:57:11.266658", "service": "scaffolder", "func_name": "update_project_status", "lineno": 71}
scaffolder-1  | {"project_id": "44045950-40d3-4d94-a5a2-3e99cdd79c8c", "repository_id": "repo-bcdad454", "event": "scaffold_job_success", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-23T01:57:11.266870", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 242}
```

## engineering-worker logs (last 30)
```
ineering-worker", "lineno": 64, "func_name": "_update_task_status"}
engineering-worker-1  | {"planning_task_id": "task-ca3f7b26", "new_status": "done", "event": "task_status_updated", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-23T02:00:07.813463", "story_id": "story-aa9e5cfe", "task_id": "eng-3fe41b4a1ebd", "project_id": "44045950-40d3-4d94-a5a2-3e99cdd79c8c", "request_id": "1aad8065-a41a-409a-8edf-2c7d61c11f2b", "correlation_id": "29e5789c-ceea-408c-a9be-e4acc6a5a036", "service": "engineering-worker", "lineno": 64, "func_name": "_update_task_status"}
engineering-worker-1  | {"task_id": "eng-3fe41b4a1ebd", "planning_task_id": "task-ca3f7b26", "skip_deploy": true, "effective_skip_deploy": true, "event": "deploy_decision", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-23T02:00:07.827986", "story_id": "story-aa9e5cfe", "project_id": "44045950-40d3-4d94-a5a2-3e99cdd79c8c", "request_id": "1aad8065-a41a-409a-8edf-2c7d61c11f2b", "correlation_id": "29e5789c-ceea-408c-a9be-e4acc6a5a036", "service": "engineering-worker", "lineno": 316, "func_name": "handle_engineering_success"}
engineering-worker-1  | {"task_id": "eng-3fe41b4a1ebd", "project_id": "44045950-40d3-4d94-a5a2-3e99cdd79c8c", "event": "deploy_skipped", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-23T02:00:07.828906", "story_id": "story-aa9e5cfe", "request_id": "1aad8065-a41a-409a-8edf-2c7d61c11f2b", "correlation_id": "29e5789c-ceea-408c-a9be-e4acc6a5a036", "service": "engineering-worker", "lineno": 391, "func_name": "handle_engineering_success"}
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 1.05s.
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 1.62s.
engineering-worker-1  | Failed to export span batch due to timeout, max retries or shutdown.
```

## scheduler logs (last 30)
```
 was the direct cause of the following exception:
scheduler-1  | 
scheduler-1  | Traceback (most recent call last):
scheduler-1  |   File "/app/src/tasks/story_completion.py", line 161, in complete_stories
scheduler-1  |     pr = await github.create_pull_request(
scheduler-1  |          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
scheduler-1  |   File "/app/shared/clients/github/_pull_requests.py", line 55, in create_pull_request
scheduler-1  |     raise RuntimeError(
scheduler-1  | RuntimeError: PR creation returned 422 but no existing PR found for story/story-aa9e5cfe->main
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=pr_review "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=pr_review "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=created "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=in_progress "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/tasks/?status=in_dev "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/tasks/?status=failed "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=deploying "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=waiting_user_secret "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=testing "HTTP/1.1 200 OK"
scheduler-1  | {"tasks_dispatched": 0, "stories_completed": 0, "scaffolds_triggered": 0, "prs_merged": 0, "event": "dispatcher_cycle", "level": "info", "logger": "src.tasks.task_dispatcher", "timestamp": "2026-07-23T02:07:47.447673", "service": "scheduler", "func_name": "task_dispatcher_loop", "lineno": 369}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.dispatch_interval_seconds "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.rag_summarizer_poll_interval "HTTP/1.1 200 OK"
```

## deploy-worker logs (last 30)
```
elf._current_context.reset(token)
deploy-worker-1  | ValueError: <Token var=<ContextVar name='current_context' default={} at 0x735793fea750> at 0x73578c07fe40> was created in a different Context
deploy-worker-1  | {"task_id": "deploy-poll-aba86bed", "result_keys": ["allocated_resources", "application_id", "deployed_url", "deployment_result", "environment_contract", "errors", "head_sha", "messages", "missing_user_secrets", "non_secret_values", "project_id", "project_spec", "provided_secrets", "repo_info", "resolution_outcome", "run_id", "secret_values", "smoke_result"], "has_smoke_result": true, "smoke_result": {"status": "pass", "checks": [{"module": "backend", "result": "pass", "detail": "HTTP 200"}]}, "deployed_url": "http://185.81.166.84:8000", "errors": [], "event": "devops_subgraph_result", "level": "info", "logger": "__main__", "timestamp": "2026-07-23T01:56:32.675407", "correlation_id": "78965d28-e0a1-4f38-a166-34cc340c59e9", "service": "deploy-worker", "request_id": "d640bf4d-0579-47a5-8010-6239404b71db", "project_id": "fab461a5-6732-4f40-9116-55537b263a05", "story_id": "story-0ba69867", "func_name": "process_deploy_job", "lineno": 374}
deploy-worker-1  | {"task_id": "deploy-poll-aba86bed", "deployed_url": "http://185.81.166.84:8000", "event": "deploy_job_success", "level": "info", "logger": "src.consumers.deploy_result_handler", "timestamp": "2026-07-23T01:56:32.675799", "correlation_id": "78965d28-e0a1-4f38-a166-34cc340c59e9", "service": "deploy-worker", "request_id": "d640bf4d-0579-47a5-8010-6239404b71db", "project_id": "fab461a5-6732-4f40-9116-55537b263a05", "story_id": "story-0ba69867", "func_name": "_handle_deploy_success", "lineno": 111}
deploy-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 0.83s.
deploy-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 1.63s.
deploy-worker-1  | Failed to export span batch due to timeout, max retries or shutdown.
```

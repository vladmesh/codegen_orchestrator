# Debug: full-llm-engineering
**Time**: 2026-07-23T01:25:28.382298+00:00

## Context
- project_id: `64885ea5-f879-43c8-867b-40e4b4f26449`
- project_name: `live-te-64885ea5f87943c8867b40e4b4f26449`
- scaffold_status: `active`
- task_id: `task-3c4c898d`
- task_status: `failed`
- story_status: `in_progress`
- final_app_status: `None`
- deployed_url: `None`
- engineering_elapsed: `35`
- deploy_run_id: `None`
- deploy_head_sha: `None`
- deploy_run_status: `None`
- deploy_outcome: `None`
- deploy_error_details: `None`
- deploy_run_error: `None`
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
scaffolder-1  | {"project_id": "64885ea5-f879-43c8-867b-40e4b4f26449", "repository_id": "repo-618c93e1", "event": "branch_protection_set", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-23T01:24:46.714297", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 231}
scaffolder-1  | HTTP Request: GET https://api.github.com/orgs/project-factory-organization/installation "HTTP/1.1 200 OK"
scaffolder-1  | HTTP Request: PATCH https://api.github.com/repos/project-factory-organization/live-te-64885ea5f87943c8867b40e4b4f26449 "HTTP/1.1 200 OK"
scaffolder-1  | {"owner": "project-factory-organization", "repo": "live-te-64885ea5f87943c8867b40e4b4f26449", "event": "repo_auto_merge_enabled", "level": "info", "logger": "shared.clients.github._actions", "timestamp": "2026-07-23T01:24:47.406942", "project_id": "64885ea5-f879-43c8-867b-40e4b4f26449", "service": "scaffolder", "func_name": "enable_repo_auto_merge", "lineno": 96}
scaffolder-1  | {"project_id": "64885ea5-f879-43c8-867b-40e4b4f26449", "repository_id": "repo-618c93e1", "event": "repo_auto_merge_enabled", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-23T01:24:47.407218", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 237}
scaffolder-1  | HTTP Request: PATCH http://api:8000/api/projects/64885ea5-f879-43c8-867b-40e4b4f26449 "HTTP/1.1 200 OK"
scaffolder-1  | {"project_id": "64885ea5-f879-43c8-867b-40e4b4f26449", "status": "active", "event": "project_status_updated", "level": "info", "logger": "src.clients.api", "timestamp": "2026-07-23T01:24:47.418724", "service": "scaffolder", "func_name": "update_project_status", "lineno": 71}
scaffolder-1  | {"project_id": "64885ea5-f879-43c8-867b-40e4b4f26449", "repository_id": "repo-618c93e1", "event": "scaffold_job_success", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-23T01:24:47.418946", "service": "scaffolder", "func_name": "_process_full_mode", "lineno": 242}
```

## engineering-worker logs (last 30)
```
ib/python3.12/site-packages/opentelemetry/context/contextvars_context.py", line 53, in detach
engineering-worker-1  |     self._current_context.reset(token)
engineering-worker-1  | ValueError: <Token var=<ContextVar name='current_context' default={} at 0x73da702e2d90> at 0x73da5ce91200> was created in a different Context
engineering-worker-1  | {"task_id": "eng-279e15c52f72", "errors": ["Development failed: Agent process failed with code 1: Claude configuration file not found at: /home/worker/.claude.json\nA backup file exists at: /home/worker/.claude/backups/.claude.json.backup.1784368790064\nYou can manually restore it by running: cp \"/home/worker/.claude/backups/.claude.json.backup.1784368790064\" \"/home/worker/.claude.json\"\n\n\nClaude configuration file not found at: /home/worker/.claude.json\nA backup file exists at: /home/worker/.claude/backups/.claude.json.backup.1784368790064\nYou can manually res..."], "event": "engineering_job_failed_status", "level": "error", "logger": "__main__", "timestamp": "2026-07-23T01:25:27.423963", "story_id": "story-453e3d03", "project_id": "64885ea5-f879-43c8-867b-40e4b4f26449", "request_id": "337374e3-9eb0-43c0-8c75-80edd922ec1a", "correlation_id": "bf60549e-38f6-46dd-a671-e54690807245", "service": "engineering-worker", "lineno": 316, "func_name": "process_engineering_job"}
engineering-worker-1  | {"planning_task_id": "task-3c4c898d", "new_status": "failed", "event": "task_status_updated", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-23T01:25:27.455216", "story_id": "story-453e3d03", "task_id": "eng-279e15c52f72", "project_id": "64885ea5-f879-43c8-867b-40e4b4f26449", "request_id": "337374e3-9eb0-43c0-8c75-80edd922ec1a", "correlation_id": "bf60549e-38f6-46dd-a671-e54690807245", "service": "engineering-worker", "lineno": 64, "func_name": "_update_task_status"}
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 1.18s.
```

## scheduler logs (last 30)
```
api/projects/ "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/tasks/?status=todo "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=in_progress "HTTP/1.1 200 OK"
scheduler-1  | {"in_progress_stories": 1, "event": "complete_stories_check", "level": "info", "logger": "src.tasks.story_completion", "timestamp": "2026-07-23T01:25:26.409400", "service": "scheduler", "func_name": "complete_stories", "lineno": 119}
scheduler-1  | HTTP Request: GET http://api:8000/api/tasks/?story_id=story-453e3d03 "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=pr_review "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=pr_review "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=created "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=in_progress "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/tasks/?status=in_dev "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/supervisor.task_stuck_threshold_minutes "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/tasks/?status=failed "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=deploying "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=waiting_user_secret "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?status=testing "HTTP/1.1 200 OK"
scheduler-1  | {"tasks_dispatched": 0, "stories_completed": 0, "scaffolds_triggered": 0, "prs_merged": 0, "event": "dispatcher_cycle", "level": "info", "logger": "src.tasks.task_dispatcher", "timestamp": "2026-07-23T01:25:26.528367", "service": "scheduler", "func_name": "task_dispatcher_loop", "lineno": 369}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.dispatch_interval_seconds "HTTP/1.1 200 OK"
```

## deploy-worker logs (last 30)
```
elf._current_context.reset(token)
deploy-worker-1  | ValueError: <Token var=<ContextVar name='current_context' default={} at 0x735793fea750> at 0x73578c25fd40> was created in a different Context
deploy-worker-1  | {"task_id": "deploy-poll-de74bd67", "result_keys": ["allocated_resources", "application_id", "deployed_url", "deployment_result", "environment_contract", "errors", "head_sha", "messages", "missing_user_secrets", "non_secret_values", "project_id", "project_spec", "provided_secrets", "repo_info", "resolution_outcome", "run_id", "secret_values", "smoke_result"], "has_smoke_result": true, "smoke_result": {"status": "pass", "checks": [{"module": "backend", "result": "pass", "detail": "HTTP 200"}]}, "deployed_url": "http://185.81.166.84:8000", "errors": [], "event": "devops_subgraph_result", "level": "info", "logger": "__main__", "timestamp": "2026-07-23T01:24:01.477153", "correlation_id": "cd50fbee-8fb9-4acf-8514-9c05b9224384", "service": "deploy-worker", "request_id": "5123e3ad-847e-43bc-b36c-415ab1638493", "project_id": "1bff269f-e34c-42f6-a8d0-07f124f23cf6", "story_id": "story-baeda68c", "func_name": "process_deploy_job", "lineno": 374}
deploy-worker-1  | {"task_id": "deploy-poll-de74bd67", "deployed_url": "http://185.81.166.84:8000", "event": "deploy_job_success", "level": "info", "logger": "src.consumers.deploy_result_handler", "timestamp": "2026-07-23T01:24:01.477378", "correlation_id": "cd50fbee-8fb9-4acf-8514-9c05b9224384", "service": "deploy-worker", "request_id": "5123e3ad-847e-43bc-b36c-415ab1638493", "project_id": "1bff269f-e34c-42f6-a8d0-07f124f23cf6", "story_id": "story-baeda68c", "func_name": "_handle_deploy_success", "lineno": 111}
deploy-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 1.02s.
deploy-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 1.64s.
deploy-worker-1  | Failed to export span batch due to timeout, max retries or shutdown.
```

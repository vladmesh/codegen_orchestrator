# Debug: full-engineering
**Time**: 2026-07-14T19:08:10.902581+00:00

## Context
- project_id: `1d9c6f88-5222-4968-b7b4-b5f3ee13a431`
- project_name: `live-test-78327ea9`
- scaffold_status: `active`
- task_id: `task-235af3da`
- task_status: `failed`
- story_status: `in_progress`
- final_app_status: `None`
- deployed_url: `None`
- engineering_elapsed: `15`

## scaffolder logs (last 30)
```
T19:07:53.307072", "service": "scaffolder", "project_id": "1d9c6f88-5222-4968-b7b4-b5f3ee13a431", "lineno": 91, "func_name": "enable_repo_auto_merge"}
scaffolder-1  | {"project_id": "1d9c6f88-5222-4968-b7b4-b5f3ee13a431", "repository_id": "repo-9bc9c697", "event": "repo_auto_merge_enabled", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-14T19:07:53.307264", "service": "scaffolder", "lineno": 237, "func_name": "_process_full_mode"}
scaffolder-1  | HTTP Request: PATCH http://api:8000/api/projects/1d9c6f88-5222-4968-b7b4-b5f3ee13a431 "HTTP/1.1 200 OK"
scaffolder-1  | {"project_id": "1d9c6f88-5222-4968-b7b4-b5f3ee13a431", "status": "active", "event": "project_status_updated", "level": "info", "logger": "src.clients.api", "timestamp": "2026-07-14T19:07:53.331418", "service": "scaffolder", "lineno": 71, "func_name": "update_project_status"}
scaffolder-1  | {"project_id": "1d9c6f88-5222-4968-b7b4-b5f3ee13a431", "repository_id": "repo-9bc9c697", "event": "scaffold_job_success", "level": "info", "logger": "src.consumer", "timestamp": "2026-07-14T19:07:53.332119", "service": "scaffolder", "lineno": 242, "func_name": "_process_full_mode"}
scaffolder-1  | {"stream": "scaffold:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:07:58.343304", "service": "scaffolder", "lineno": 251, "func_name": "_iter_entries"}
scaffolder-1  | {"stream": "scaffold:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:08:04.351773", "service": "scaffolder", "lineno": 251, "func_name": "_iter_entries"}
scaffolder-1  | {"stream": "scaffold:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:08:10.361005", "service": "scaffolder", "lineno": 251, "func_name": "_iter_entries"}
```

## engineering-worker logs (last 30)
```
le \"/app/shared/clients/github/_base.py\", line 100, in _load_private_key\n    self._private_key = os.getenv(\"GITHUB_PRIVATE_KEY_CONTENT\")\nFileNotFoundError: GitHub App private key not found at /app/secrets/github_app_key.pem"}
engineering-worker-1  | Failed to detach context
engineering-worker-1  | Traceback (most recent call last):
engineering-worker-1  |   File "/usr/local/lib/python3.12/site-packages/opentelemetry/context/__init__.py", line 155, in detach
engineering-worker-1  |     _RUNTIME_CONTEXT.detach(token)
engineering-worker-1  |   File "/usr/local/lib/python3.12/site-packages/opentelemetry/context/contextvars_context.py", line 53, in detach
engineering-worker-1  |     self._current_context.reset(token)
engineering-worker-1  | ValueError: <Token var=<ContextVar name='current_context' default={} at 0x7f7301fb68e0> at 0x7f72fc7cbf40> was created in a different Context
engineering-worker-1  | {"task_id": "eng-1536f3e1a4b2", "errors": ["Developer error: GitHub App private key not found at /app/secrets/github_app_key.pem"], "event": "engineering_job_failed_status", "level": "error", "logger": "__main__", "timestamp": "2026-07-14T19:08:09.367463", "request_id": "69792430-f19c-4817-8315-b8634da88e29", "story_id": "story-7e57bfdd", "service": "engineering-worker", "project_id": "1d9c6f88-5222-4968-b7b4-b5f3ee13a431", "correlation_id": "53244019-cddd-46c1-91d9-64273c1e4498", "lineno": 315, "func_name": "process_engineering_job"}
engineering-worker-1  | {"planning_task_id": "task-235af3da", "new_status": "failed", "event": "task_status_updated", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-14T19:08:09.416205", "request_id": "69792430-f19c-4817-8315-b8634da88e29", "story_id": "story-7e57bfdd", "service": "engineering-worker", "project_id": "1d9c6f88-5222-4968-b7b4-b5f3ee13a431", "task_id": "eng-1536f3e1a4b2", "correlation_id": "53244019-cddd-46c1-91d9-64273c1e4498", "lineno": 63, "func_name": "_update_task_status"}
```

## scheduler logs (last 30)
```
  resp.raise_for_status()\n  File \"/usr/local/lib/python3.12/site-packages/httpx/_models.py\", line 829, in raise_for_status\n    raise HTTPStatusError(message, request=request, response=self)\nhttpx.HTTPStatusError: Client error '401 Unauthorized' for url 'https://billing.time4vps.com/api/server'\nFor more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/401"}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.server_details_sync_interval "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.provisioning_stuck_timeout_seconds "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.provisioning_trigger_cooldown_seconds "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers "HTTP/1.1 307 Temporary Redirect"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers/ "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers "HTTP/1.1 307 Temporary Redirect"
scheduler-1  | HTTP Request: GET http://api:8000/api/servers/ "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/incidents/active "HTTP/1.1 200 OK"
scheduler-1  | {"servers_discovered": 0, "servers_updated": 0, "servers_missing": 0, "details_updated": 0, "triggers_published": 0, "incidents_resolved": 0, "duration_sec": 0.36, "event": "server_sync_complete", "level": "info", "logger": "src.tasks.server_sync", "timestamp": "2026-07-14T19:08:03.154562", "service": "scheduler", "func_name": "sync_servers_worker", "lineno": 107}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.server_sync_interval "HTTP/1.1 200 OK"
scheduler-1  | {"stream": "provisioner:results", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:08:06.790016", "service": "scheduler", "func_name": "_iter_entries", "lineno": 251}
```

## deploy-worker logs (last 30)
```
": "_iter_entries"}
deploy-worker-1  | {"stream": "deploy:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:07:32.716009", "service": "deploy-worker", "lineno": 251, "func_name": "_iter_entries"}
deploy-worker-1  | {"stream": "deploy:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:07:38.725864", "service": "deploy-worker", "lineno": 251, "func_name": "_iter_entries"}
deploy-worker-1  | {"stream": "deploy:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:07:44.735222", "service": "deploy-worker", "lineno": 251, "func_name": "_iter_entries"}
deploy-worker-1  | {"stream": "deploy:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:07:50.740279", "service": "deploy-worker", "lineno": 251, "func_name": "_iter_entries"}
deploy-worker-1  | {"stream": "deploy:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:07:56.752199", "service": "deploy-worker", "lineno": 251, "func_name": "_iter_entries"}
deploy-worker-1  | {"stream": "deploy:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:08:02.766202", "service": "deploy-worker", "lineno": 251, "func_name": "_iter_entries"}
deploy-worker-1  | {"stream": "deploy:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:08:08.774219", "service": "deploy-worker", "lineno": 251, "func_name": "_iter_entries"}
```

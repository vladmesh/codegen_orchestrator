# Debug: full-engineering
**Time**: 2026-07-14T19:21:35.664395+00:00

## Context
- project_id: `9210bb60-a2f3-48ba-9d94-be1784ec36c4`
- project_name: `live-test-8e9881d3`
- scaffold_status: `active`
- task_id: `task-add8bdb4`
- task_status: `failed`
- story_status: `in_progress`
- final_app_status: `None`
- deployed_url: `None`
- engineering_elapsed: `60`

## scaffolder logs (last 30)
```
", "lineno": 251, "func_name": "_iter_entries"}
scaffolder-1  | {"stream": "scaffold:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:20:55.051887", "service": "scaffolder", "lineno": 251, "func_name": "_iter_entries"}
scaffolder-1  | {"stream": "scaffold:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:21:01.062249", "service": "scaffolder", "lineno": 251, "func_name": "_iter_entries"}
scaffolder-1  | {"stream": "scaffold:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:21:07.074071", "service": "scaffolder", "lineno": 251, "func_name": "_iter_entries"}
scaffolder-1  | {"stream": "scaffold:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:21:13.086225", "service": "scaffolder", "lineno": 251, "func_name": "_iter_entries"}
scaffolder-1  | {"stream": "scaffold:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:21:19.098450", "service": "scaffolder", "lineno": 251, "func_name": "_iter_entries"}
scaffolder-1  | {"stream": "scaffold:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:21:25.105385", "service": "scaffolder", "lineno": 251, "func_name": "_iter_entries"}
scaffolder-1  | {"stream": "scaffold:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:21:31.116634", "service": "scaffolder", "lineno": 251, "func_name": "_iter_entries"}
```

## engineering-worker logs (last 30)
```
emetry/context/contextvars_context.py", line 53, in detach
engineering-worker-1  |     self._current_context.reset(token)
engineering-worker-1  | ValueError: <Token var=<ContextVar name='current_context' default={} at 0x7c64f23b3bf0> at 0x7c64ef030dc0> was created in a different Context
engineering-worker-1  | {"task_id": "eng-76850aedd2af", "errors": ["Development failed: Agent exited without reporting result"], "event": "engineering_job_failed_status", "level": "error", "logger": "__main__", "timestamp": "2026-07-14T19:21:30.588896", "correlation_id": "db61d2e4-f42f-4bad-8442-d12f5cdbee0b", "story_id": "story-f47a0b8e", "project_id": "9210bb60-a2f3-48ba-9d94-be1784ec36c4", "request_id": "bf9704eb-f53a-4d2a-85e8-b898017d1ef7", "service": "engineering-worker", "func_name": "process_engineering_job", "lineno": 315}
engineering-worker-1  | {"planning_task_id": "task-add8bdb4", "new_status": "failed", "event": "task_status_updated", "level": "info", "logger": "src.consumers.engineering_result_handler", "timestamp": "2026-07-14T19:21:30.626750", "correlation_id": "db61d2e4-f42f-4bad-8442-d12f5cdbee0b", "story_id": "story-f47a0b8e", "project_id": "9210bb60-a2f3-48ba-9d94-be1784ec36c4", "task_id": "eng-76850aedd2af", "request_id": "bf9704eb-f53a-4d2a-85e8-b898017d1ef7", "service": "engineering-worker", "func_name": "_update_task_status", "lineno": 63}
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 1.16s.
engineering-worker-1  | Transient error Internal Server Error encountered while exporting span batch, retrying in 2.06s.
engineering-worker-1  | {"stream": "engineering:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:21:35.629400", "service": "engineering-worker", "func_name": "_iter_entries", "lineno": 251}
engineering-worker-1  | Failed to export span batch due to timeout, max retries or shutdown.
```

## scheduler logs (last 30)
```
service": "scheduler", "lineno": 234, "func_name": "task_dispatcher_loop"}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.dispatch_interval_seconds "HTTP/1.1 200 OK"
scheduler-1  | {"stream": "provisioner:results", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:21:23.592549", "service": "scheduler", "lineno": 251, "func_name": "_iter_entries"}
scheduler-1  | {"server_handle": "vps-273978", "server_ip": "185.81.166.84", "reason": "node_exporter_fetch_failed", "event": "server_unreachable", "level": "warning", "logger": "src.tasks.health_checker", "timestamp": "2026-07-14T19:21:28.658668", "service": "scheduler", "lineno": 91, "func_name": "_check_server"}
scheduler-1  | HTTP Request: GET http://api:8000/api/incidents/?server_handle=vps-273978&incident_type=server_unreachable&status=detected "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/applications/ "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/health.metrics_cleanup_interval_seconds "HTTP/1.1 200 OK"
scheduler-1  | {"servers_checked": 1, "duration_sec": 10.12, "event": "health_check_cycle_complete", "level": "info", "logger": "src.tasks.health_checker", "timestamp": "2026-07-14T19:21:28.722685", "service": "scheduler", "lineno": 323, "func_name": "health_check_worker"}
scheduler-1  | {"stream": "provisioner:results", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:21:29.603156", "service": "scheduler", "lineno": 251, "func_name": "_iter_entries"}
scheduler-1  | {"stream": "provisioner:results", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:21:35.609652", "service": "scheduler", "lineno": 251, "func_name": "_iter_entries"}
```

## deploy-worker logs (last 30)
```
es", "lineno": 251}
deploy-worker-1  | {"stream": "deploy:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:20:57.328641", "service": "deploy-worker", "func_name": "_iter_entries", "lineno": 251}
deploy-worker-1  | {"stream": "deploy:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:21:03.341565", "service": "deploy-worker", "func_name": "_iter_entries", "lineno": 251}
deploy-worker-1  | {"stream": "deploy:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:21:09.348453", "service": "deploy-worker", "func_name": "_iter_entries", "lineno": 251}
deploy-worker-1  | {"stream": "deploy:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:21:15.360194", "service": "deploy-worker", "func_name": "_iter_entries", "lineno": 251}
deploy-worker-1  | {"stream": "deploy:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:21:21.370188", "service": "deploy-worker", "func_name": "_iter_entries", "lineno": 251}
deploy-worker-1  | {"stream": "deploy:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:21:27.381242", "service": "deploy-worker", "func_name": "_iter_entries", "lineno": 251}
deploy-worker-1  | {"stream": "deploy:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T19:21:33.396186", "service": "deploy-worker", "func_name": "_iter_entries", "lineno": 251}
```

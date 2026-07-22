# Debug: full-scaffold
**Time**: 2026-07-14T16:12:19.240175+00:00

## Context
- project_id: `bb99e7db-3578-41c2-b45c-3429becbf4ff`
- project_name: `live-test-e52efe7c`
- scaffold_status: `draft`
- task_id: `None`
- task_status: `None`
- story_status: `None`
- final_app_status: `None`
- deployed_url: `None`
- engineering_elapsed: `None`

## scaffolder logs (last 30)
```
", "func_name": "_iter_entries", "lineno": 251}
scaffolder-1  | {"stream": "scaffold:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T16:11:39.723233", "service": "scaffolder", "func_name": "_iter_entries", "lineno": 251}
scaffolder-1  | {"stream": "scaffold:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T16:11:45.737215", "service": "scaffolder", "func_name": "_iter_entries", "lineno": 251}
scaffolder-1  | {"stream": "scaffold:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T16:11:51.747274", "service": "scaffolder", "func_name": "_iter_entries", "lineno": 251}
scaffolder-1  | {"stream": "scaffold:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T16:11:57.756230", "service": "scaffolder", "func_name": "_iter_entries", "lineno": 251}
scaffolder-1  | {"stream": "scaffold:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T16:12:03.768919", "service": "scaffolder", "func_name": "_iter_entries", "lineno": 251}
scaffolder-1  | {"stream": "scaffold:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T16:12:09.782296", "service": "scaffolder", "func_name": "_iter_entries", "lineno": 251}
scaffolder-1  | {"stream": "scaffold:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T16:12:15.792198", "service": "scaffolder", "func_name": "_iter_entries", "lineno": 251}
```

## engineering-worker logs (last 30)
```
m redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T16:11:41.000177", "service": "engineering-worker", "lineno": 251, "func_name": "_iter_entries"}
engineering-worker-1  | {"stream": "engineering:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T16:11:47.013543", "service": "engineering-worker", "lineno": 251, "func_name": "_iter_entries"}
engineering-worker-1  | {"stream": "engineering:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T16:11:53.021162", "service": "engineering-worker", "lineno": 251, "func_name": "_iter_entries"}
engineering-worker-1  | {"stream": "engineering:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T16:11:59.036616", "service": "engineering-worker", "lineno": 251, "func_name": "_iter_entries"}
engineering-worker-1  | {"stream": "engineering:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T16:12:05.049257", "service": "engineering-worker", "lineno": 251, "func_name": "_iter_entries"}
engineering-worker-1  | {"stream": "engineering:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T16:12:11.058200", "service": "engineering-worker", "lineno": 251, "func_name": "_iter_entries"}
engineering-worker-1  | {"stream": "engineering:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T16:12:17.068147", "service": "engineering-worker", "lineno": 251, "func_name": "_iter_entries"}
```

## scheduler logs (last 30)
```
s "HTTP/1.1 307 Temporary Redirect"
scheduler-1  | HTTP Request: GET http://api:8000/api/projects/ "HTTP/1.1 200 OK"
scheduler-1  | HTTP Request: GET http://api:8000/api/stories/?project_id=bb99e7db-3578-41c2-b45c-3429becbf4ff "HTTP/1.1 200 OK"
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
scheduler-1  | {"tasks_dispatched": 0, "stories_completed": 0, "scaffolds_triggered": 0, "prs_merged": 0, "event": "dispatcher_cycle", "level": "info", "logger": "src.tasks.task_dispatcher", "timestamp": "2026-07-14T16:12:15.153261", "service": "scheduler", "func_name": "task_dispatcher_loop", "lineno": 234}
scheduler-1  | HTTP Request: GET http://api:8000/api/system-configs/scheduler.dispatch_interval_seconds "HTTP/1.1 200 OK"
scheduler-1  | {"stream": "provisioner:results", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T16:12:19.418184", "service": "scheduler", "func_name": "_iter_entries", "lineno": 251}
scheduler-1  | HTTP Request: GET http://api:8000/api/api-keys/time4vps "HTTP/1.1 200 OK"
```

## deploy-worker logs (last 30)
```
": "_iter_entries"}
deploy-worker-1  | {"stream": "deploy:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T16:11:42.226908", "service": "deploy-worker", "lineno": 251, "func_name": "_iter_entries"}
deploy-worker-1  | {"stream": "deploy:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T16:11:48.242212", "service": "deploy-worker", "lineno": 251, "func_name": "_iter_entries"}
deploy-worker-1  | {"stream": "deploy:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T16:11:54.255151", "service": "deploy-worker", "lineno": 251, "func_name": "_iter_entries"}
deploy-worker-1  | {"stream": "deploy:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T16:12:00.264183", "service": "deploy-worker", "lineno": 251, "func_name": "_iter_entries"}
deploy-worker-1  | {"stream": "deploy:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T16:12:06.277237", "service": "deploy-worker", "lineno": 251, "func_name": "_iter_entries"}
deploy-worker-1  | {"stream": "deploy:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T16:12:12.293291", "service": "deploy-worker", "lineno": 251, "func_name": "_iter_entries"}
deploy-worker-1  | {"stream": "deploy:queue", "error": "Timeout reading from redis:6379", "event": "consume_error", "level": "error", "logger": "shared.redis.client", "timestamp": "2026-07-14T16:12:18.307177", "service": "deploy-worker", "lineno": 251, "func_name": "_iter_entries"}
```

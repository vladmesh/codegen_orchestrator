# Logging Guide

> Structured logging implementation based on `structlog` with JSON output for Grafana Loki.

## Quick Start

```python
from shared.logging_config import setup_logging
import structlog

# Initialize at service startup
setup_logging(service_name="my_service")

# Get logger and use it
logger = structlog.get_logger()
logger.info("event_name", key1="value1", key2=123)
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | Logging level: DEBUG, INFO, WARNING, ERROR |
| `LOG_FORMAT` | `console` | Output format: `json` (production) or `console` (dev) |
| `SERVICE_NAME` | `unknown` | Service name added to all logs |

### Example Output

**Console format (development):**
```
2025-12-26T12:00:00 [info] event_name    key1=value1 key2=123 service=api
```

**JSON format (production):**
```json
{
  "timestamp": "2025-12-26T12:00:00.123456+02:00",
  "level": "info",
  "service": "api",
  "event": "event_name",
  "key1": "value1",
  "key2": 123
}
```

---

## Logging Patterns

### Basic Logging

```python
import structlog

logger = structlog.get_logger()

# Info log with context
logger.info("creating_project", project_id="proj_123", user_id=456)

# Warning log
logger.warning("rate_limit_approaching", current=90, limit=100)

# Error log with exception
try:
    do_something()
except Exception as e:
    logger.error("operation_failed",
        error=str(e),
        error_type=type(e).__name__,
        exc_info=True)  # Includes stack trace
```

### Context Propagation

Use `contextvars` to bind context that persists across function calls:

```python
import structlog

# Bind context for all subsequent logs in this request
structlog.contextvars.bind_contextvars(
    correlation_id="msg_123_1735167345",
    user_id=123
)

# All logs now include correlation_id and user_id
logger.info("step_one")  # Has correlation_id, user_id
logger.info("step_two")  # Has correlation_id, user_id

# Clear context when done
structlog.contextvars.unbind_contextvars("correlation_id", "user_id")
```

### LangGraph Node Logging

Use the `@log_node_execution` decorator for automatic node tracking:

```python
from nodes.base import log_node_execution
import structlog

logger = structlog.get_logger()

@log_node_execution("my_node")
async def my_node(state: dict) -> dict:
    # Logs "node_start" automatically
    
    logger.info("doing_work", item_count=len(items))
    
    # Logs "node_complete" with duration on success
    # Logs "node_failed" with error on exception
    return {"key": "value"}
```

---

## Standard Events Reference

### API Service

| Event | Level | Description | Context Fields |
|-------|-------|-------------|----------------|
| `http_request` | info | HTTP request completed | `method`, `path`, `status_code`, `duration_ms` |
| `creating_project` | info | Project creation started | `project_id`, `name` |
| `project_updated` | info | Project updated | `project_id`, `status` |
| `project_patched` | info | Project patched | `project_id`, `status` |
| `project_creation_failed_duplicate` | warning | Duplicate project ID | `project_id` |
| `openrouter_fetching_models` | info | Fetching models from OpenRouter | — |
| `openrouter_models_cached` | info | Models cached | `model_count` |
| `openrouter_fetch_failed` | error | OpenRouter API failed | `error`, `error_type` |

### LangGraph Worker

| Event | Level | Description | Context Fields |
|-------|-------|-------------|----------------|
| `message_received` | info | Message from Telegram | `chat_id`, `message_length` |
| `node_start` | info | Node execution started | `node` |
| `node_complete` | info | Node execution completed | `node`, `duration_ms`, `state_updates` |
| `node_failed` | error | Node execution failed | `node`, `duration_ms`, `error`, `error_type` |
| `spawning_developer_worker` | info | Developer worker spawn | `repo_name` |
| `spawning_factory_worker` | info | Factory worker spawn | `repo` |
| `service_deployment_record_created` | info | Deployment recorded | `service_name` |
| `unknown_tool_called` | warning | Unknown tool requested | `tool_name` |
| `conversation_history_cleared` | info | Thread history cleared | — |

### Provisioner

| Event | Level | Description | Context Fields |
|-------|-------|-------------|----------------|
| `password_reset_triggered` | info | Password reset started | `server_handle`, `server_id` |
| `password_reset_completed` | info | Password reset done | `server_handle` |
| `password_reset_timeout` | error | Password reset timeout | `error` |
| `os_reinstall_start` | info | OS reinstall started | `server_handle`, `server_id` |
| `reinstall_task_created` | info | Reinstall task queued | `task_id` |
| `os_reinstall_completed` | info | OS reinstall done | `server_handle` |
| `reinstall_timeout` | error | Reinstall timeout | `error` |
| `ssh_access_ok` | info | SSH connection success | `server_handle` |
| `ssh_access_failed` | info | SSH connection failed | `server_handle` |
| `service_redeployment_start` | info | Redeployment started | `server_handle` |
| `services_found_for_redeployment` | info | Services to redeploy | `server_handle`, `count` |
| `service_redeployed` | info | Service redeployed | `service_name` |
| `service_redeploy_failed` | error | Redeployment failed | `service_name`, `error` |
| `ansible_stderr` | warning | Ansible stderr output | `output` |
| `ansible_playbook_timeout` | error | Ansible timeout | `playbook`, `timeout` |

### Scheduler

| Event | Level | Description | Context Fields |
|-------|-------|-------------|----------------|
| `scheduler_started` | info | Scheduler started | — |
| `scheduler_shutdown_requested` | info | Shutdown signal | — |
| `health_check_start` | info | Health check started | `servers_count` |
| `server_healthy` | debug | Server is healthy | `server_handle` |
| `incident_recovery_triggered` | info | Recovery triggered | `server_handle` |
| `github_sync_start` | info | GitHub sync started | `org_name` |
| `github_repos_fetched` | info | Repos fetched | `org_name`, `repo_count` |
| `server_sync_worker_started` | info | Server sync started | — |
| `server_reappeared` | info | Server back online | `server_ip` |
| `server_missing_from_time4vps` | warning | Server not in provider | `server_ip` |
| `server_details_sync_start` | info | Details sync started | — |
| `server_details_sync_complete` | info | Details sync done | `updated_count` |
| `server_pending_setup_trigger` | info | Setup triggered | `server_handle` |

### Telegram Bot

| Event | Level | Description | Context Fields |
|-------|-------|-------------|----------------|
| `telegram_bot_starting` | info | Bot starting | — |
| `message_received` | info | Telegram message received | `user_id`, `chat_id`, `correlation_id` |
| `message_published` | info | Published to Redis | `stream` |
| `sending_message` | info | Sending response | `chat_id`, `reply_to_message_id` |
| `message_sent` | info | Response sent | `chat_id` |
| `invalid_outgoing_message` | warning | Invalid message format | `payload` |

### Worker Spawner

| Event | Level | Description | Context Fields |
|-------|-------|-------------|----------------|
| `spawn_request_received` | info | Spawn request | `request_id`, `repo`, `branch` |
| `docker_container_creating` | info | Container creating | `request_id`, `image` |
| `docker_container_created` | info | Container created | `request_id`, `container_id` |
| `worker_execution_complete` | info | Worker finished | `request_id`, `exit_code`, `duration_sec` |
| `spawn_result_published` | info | Result published | `request_id`, `channel` |

---

## Querying Logs

### Docker Compose + jq

```bash
# All logs in JSON
docker compose logs -f api | jq

# Filter by event
docker compose logs langgraph | jq 'select(.event=="node_start")'

# Filter by level
docker compose logs | jq 'select(.level=="error")'

# Filter by service
docker compose logs | jq 'select(.service=="scheduler")'

# Trace by correlation_id
docker compose logs | jq 'select(.correlation_id=="msg_123_1735167345")'

# Filter by node
docker compose logs langgraph | jq 'select(.node=="developer")'

# Find slow operations
docker compose logs | jq 'select(.duration_ms > 1000)'

# Errors with stack traces
docker compose logs | jq 'select(.level=="error") | {event, error, error_type}'
```

### Grafana Loki (LogQL)

```logql
# All logs from service
{service="langgraph"}

# Specific node
{service="langgraph"} | json | node="developer"

# Trace request
{job="docker"} | json | correlation_id="msg_123_1735167345"

# Errors in last hour
{job="docker"} | json | level="error"

# Slow operations (>1s)
{job="docker"} | json | duration_ms > 1000

# Count events by type
sum by (event) (count_over_time({service="api"} | json [1h]))
```

---

## Best Practices

### DO

```python
# Use snake_case event names
logger.info("user_created", user_id=123)

# Include relevant context
logger.info("deployment_complete", 
    server_handle="main-1",
    duration_sec=45.2,
    services_count=3)

# Log errors with full context
logger.error("api_call_failed",
    url=url,
    status_code=response.status_code,
    error=response.text[:200],
    exc_info=True)
```

### DON'T

```python
# Don't use f-strings for dynamic content
logger.info(f"Created user {user_id}")  # BAD

# Don't log sensitive data
logger.info("login", password=password)  # BAD

# Don't use generic event names
logger.info("done")  # BAD
logger.info("error")  # BAD
```

---

## Troubleshooting

### Logs not appearing

1. Check `LOG_LEVEL` - set to `DEBUG` for more output
2. Verify `setup_logging()` is called before any logging

### JSON parsing fails

1. Ensure `LOG_FORMAT=json` is set
2. Check for print() statements mixed with logs

### Missing context (correlation_id, etc.)

1. Verify `bind_contextvars()` is called before logging
2. Check that context is bound in the correct async context

### Performance issues

1. Avoid logging large objects (truncate if needed)
2. Use `DEBUG` level for high-frequency logs
3. Set `LOG_LEVEL=INFO` in production




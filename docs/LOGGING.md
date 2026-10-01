# Logging Guide

> Structured logging implementation based on `structlog` with JSON output for Grafana Loki.

## Collection Architecture

```
Services (structlog JSON) → stdout → Docker → Promtail → Loki → Grafana
```

### Stack
- **Loki** (port 3100) — log aggregation, 7-day retention
- **Promtail** — scrapes Docker container logs, ships to Loki
- **Grafana** (port 3000) — dashboards, log viewer, alerting

All services output `LOG_FORMAT=json` in Docker. Promtail reads log files
non-destructively (`docker logs` still works).

### Correlation ID propagation
- `BaseMessage` (shared/contracts/base.py) auto-generates `correlation_id` per message
- All consumers bind it via `bind_message_context()` on message receipt
- The shared transport (`shared/clients/internal_api.py`) puts `X-Correlation-ID` on every internal
  API request; a flow that bound none — a bot update, a scheduler loop — gets one created and bound
  there, so the rest of that flow reuses it
- API middleware binds it on receipt
- Result: filter by `correlation_id` in Grafana → see full request flow across all services

### Infrastructure files
```
infra/
├── loki.yml                          # Loki config (TSDB, 7-day retention)
├── promtail.yml                      # Scrape Docker container logs
└── grafana/
    ├── datasources.yml               # Auto-provision Loki datasource
    ├── dashboards.yml                # Dashboard provisioning config
    └── dashboards/service-logs.json  # Pre-built "Service Logs" dashboard
```

## Quick Start

```python
from shared.log_config import setup_logging
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
| `LOKI_URL` | — | Loki **read** address for log consumers (`http://loki:3100`). `scheduler-maintenance` refuses to start without it; pipeline and infrastructure do not read it. |

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
    logger.error(
        "operation_failed", error=str(e), error_type=type(e).__name__, exc_info=True
    )  # Includes stack trace
```

### Context Propagation

Use `contextvars` to bind context that persists across function calls:

```python
import structlog

# Bind context for all subsequent logs in this request
structlog.contextvars.bind_contextvars(correlation_id="msg_123_1735167345", user_id=123)

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

## Event names and fields

Event names are implementation telemetry, not a stable cross-service API. Do not maintain an exhaustive
copied event catalogue here: it drifts as workflows are renamed. The source of truth is the literal event
name passed to `structlog` in the service that emits it.

When adding or changing an event:

- use a stable `snake_case` name describing the event, not prose;
- include identifiers needed to correlate the operation (`correlation_id`, project/story/task/run/worker ids as applicable);
- log typed outcomes/dispositions rather than secret-bearing payloads;
- update dashboards/alerts/tests that explicitly query that event name in the same change.

To discover current events, search the relevant service for `logger.info(`, `logger.warning(`,
`logger.error(`, and `logger.exception(`. Cross-service tracing should rely on correlation/id fields,
not on an assumed global event vocabulary.

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
docker compose logs scheduler-pipeline scheduler-infrastructure scheduler-maintenance

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
logger.info("deployment_complete", server_handle="main-1", duration_sec=45.2, services_count=3)

# Log errors with full context
logger.error(
    "api_call_failed",
    url=url,
    status_code=response.status_code,
    error=response.text[:200],
    exc_info=True,
)
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


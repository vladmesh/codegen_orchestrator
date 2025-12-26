# Structured Logging Implementation Plan

> **Goal:** Систематизация логирования для покрытия всего функционала с единообразным форматом и поддержкой Grafana.

## Overview

Внедрение структурированного логирования на базе `structlog` с поддержкой JSON-формата для Grafana Loki. Логи будут содержать обязательные поля (timestamp, level, service, node) и контекстные данные для фильтрации и трассировки.

**Timeline:** 7 дней (5 фаз)  
**Effort:** ~20-25 часов  
**Risk Level:** Low-Medium (backward compatible, постепенное внедрение)

---

## Current State Analysis

### Microservices

| Service | Status | Logging Approach | Issues |
|---------|--------|------------------|--------|
| **api** | ✅ | `logging.basicConfig()` | Нет контекста (user_id, project_id) |
| **langgraph** | ✅ | `logging.basicConfig()` | Нет информации о nodes, thread_id |
| **scheduler** | ✅ | Basic logging | Нет метрик (duration, items processed) |
| **telegram_bot** | ✅ | Basic logging | Нет correlation_id |
| **worker-spawner** | ✅ | Basic logging | Нет lifecycle tracking |
| **coding-worker** | ⚠️ | Stdout only | Нет интеграции |
| **infrastructure** | ⚠️ | Ansible logs | Нужна интеграция |

### Key Problems

1. ❌ **Неструктурированные логи** - f-strings вместо key-value pairs
2. ❌ **Нет context propagation** - невозможно проследить запрос через сервисы
3. ❌ **Разные форматы** - каждый сервис логирует по-своему
4. ❌ **Нет поддержки JSON** - сложно парсить в Grafana
5. ❌ **Нет node/task metadata** - в LangGraph логах не видно какой узел работает

---

## Target Architecture

### Logging Stack

```
┌─────────────────┐
│   Microservice  │
│   (structlog)   │
│       ↓         │
│   JSON output   │ ──────┐
└─────────────────┘       │
                          ▼
                   ┌──────────────┐
                   │ Grafana Loki │
                   │  (storage)   │
                   └──────────────┘
                          ▲
                          │
                   ┌──────────────┐
                   │   Grafana    │
                   │  (queries)   │
                   └──────────────┘
```

### Log Entry Structure

**Обязательные поля:**
```json
{
  "timestamp": "2025-12-26T02:03:45.123456+02:00",
  "level": "info",
  "service": "langgraph",
  "logger": "nodes.developer",
  "event": "spawning_worker"
}
```

**Контекстные поля (опциональные):**
```json
{
  "node": "developer",
  "thread_id": "user_123",
  "correlation_id": "msg_456_1735167345",
  "user_id": 123,
  "project_id": "palindrome_bot",
  "duration_ms": 250,
  "error": "Connection timeout"
}
```

### Configuration via Environment

```bash
# .env
LOG_LEVEL=INFO              # DEBUG, INFO, WARNING, ERROR
LOG_FORMAT=json             # json, console
SERVICE_NAME=langgraph      # Auto-set in docker-compose
```

---

## Implementation Phases

### Phase 1: Infrastructure Setup (Done)

**Goal:** Create shared logging module and test basic configuration

#### Tasks

1. ✅ **Create shared logging module**
   - File: `shared/logging_config.py`
   - Dependencies: `structlog>=25.1.0`
   - Processors: timestamp, level, service_name, logger_name
   - Renderers: JSON (production), Console (development)

2. ✅ **Add dependencies**
   - Update `shared/pyproject.toml`
   - Update service-specific `pyproject.toml` files

3. ✅ **Create test suite**
   - File: `shared/tests/test_logging_config.py`
   - Test JSON output format
   - Test console output format
   - Test context injection

#### Deliverables

- ✅ `shared/logging_config.py` with `setup_logging()` function
- ✅ Unit tests passing
- ✅ Documentation with usage examples


#### Acceptance Criteria

```python
# Example usage
from shared.logging_config import setup_logging
import structlog

setup_logging(service_name="test", log_format="json")
logger = structlog.get_logger()
logger.info("test_event", user_id=123)
# Output: {"timestamp": "...", "level": "info", "service": "test", "event": "test_event", "user_id": 123}
```

---

### Phase 2: Core Services Migration (Done)

**Goal:** Migrate API and LangGraph worker to structured logging

#### 2.1: API Service (API endpoints and middleware)

**Files to modify:**
- `services/api/src/main.py`
- `services/api/src/routers/*.py`
- `services/api/src/clients/*.py`
- `services/api/src/tasks/*.py` (if any remain)

**Changes:**

1. ✅ **Main.py - Add correlation middleware**
   ```python
   @app.middleware("http")
   async def correlation_middleware(request: Request, call_next):
       correlation_id = request.headers.get(
           "X-Correlation-ID", 
           f"req_{uuid.uuid4().hex[:8]}"
       )
       structlog.contextvars.bind_contextvars(
           correlation_id=correlation_id,
           method=request.method,
           path=request.url.path
       )
       
       start = time.time()
       response = await call_next(request)
       duration_ms = (time.time() - start) * 1000
       
       logger.info("http_request",
           status_code=response.status_code,
           duration_ms=round(duration_ms, 2))
       
       return response
   ```

2. ✅ **Routers - Structured logging**
   ```python
   # Before
   logger.info(f"Creating project for user {user_id}")
   
   # After
   logger.info("creating_project", 
       user_id=user_id, 
       project_name=request.name)
   ```

3. ✅ **Error logging**
   ```python
   # Before
   logger.error(f"Failed to create project: {e}")
   
   # After
   logger.error("project_creation_failed",
       error=str(e),
       error_type=type(e).__name__,
       exc_info=True)
   ```

**Key Events to Log:**
- `server_created`, `server_updated`, `server_deleted`
- `project_created`, `project_activated`
- `deployment_recorded`
- `agent_config_fetched`
- API call errors

#### 2.2: LangGraph Worker

**Files to modify:**
- `services/langgraph/src/worker.py`
- `services/langgraph/src/graph.py`

**Changes:**

1. ✅ **Worker.py - Message processing**
   ```python
   logger.info("message_received",
       user_id=user_id,
       chat_id=chat_id,
       thread_id=thread_id,
       message_length=len(text),
       correlation_id=data.get("correlation_id"))
   
   # Bind context for all subsequent logs
   structlog.contextvars.bind_contextvars(
       thread_id=thread_id,
       correlation_id=data.get("correlation_id")
   )
   
   start = time.time()
   result = await graph.ainvoke(state, config)
   duration = (time.time() - start) * 1000
   
   logger.info("graph_execution_complete",
       duration_ms=round(duration, 2),
       response_length=len(response_text))
   ```

2. ✅ **Provisioner trigger logging**
   ```python
   logger.info("provisioner_trigger_received",
       server_handle=server_handle,
       is_incident_recovery=is_incident_recovery)
   ```

**Key Events:**
- `message_received`
- `graph_execution_start`
- `graph_execution_complete`
- `graph_execution_failed`
- `provisioner_trigger_received`
- `conversation_history_cleared`

#### Deliverables

- ✅ API logs in structured format
- ✅ LangGraph worker logs with thread_id context
- ✅ Correlation ID propagation working
- ✅ All services start without errors

#### Acceptance Criteria

1. All HTTP requests logged with duration and status
2. LangGraph execution has thread_id in all logs
3. `docker compose logs -f api | jq` shows valid JSON
4. Can filter logs: `docker compose logs api | jq 'select(.event=="creating_project")'`

---

### Phase 3: LangGraph Nodes Migration (Days 4-5)

**Goal:** Add structured logging to all agent nodes with automatic context injection

#### 3.1: Base Agent Node

**File:** `services/langgraph/src/nodes/base.py`

**Changes:**

1. **Create node execution decorator**
   ```python
   from functools import wraps
   import structlog
   import time
   
   def log_node_execution(node_name: str):
       """Decorator to log node start/end and inject context."""
       def decorator(func):
           @wraps(func)
           async def wrapper(state: dict):
               logger = structlog.get_logger()
               
               # Inject node context
               structlog.contextvars.bind_contextvars(
                   node=node_name,
                   thread_id=state.get("thread_id")
               )
               
               logger.info("node_start")
               start = time.time()
               
               try:
                   result = await func(state)
                   duration = (time.time() - start) * 1000
                   
                   logger.info("node_complete",
                       duration_ms=round(duration, 2),
                       state_updates=list(result.keys()) if result else [])
                   
                   return result
                   
               except Exception as e:
                   duration = (time.time() - start) * 1000
                   
                   logger.error("node_failed",
                       duration_ms=round(duration, 2),
                       error=str(e),
                       error_type=type(e).__name__,
                       exc_info=True)
                   raise
               finally:
                   # Clear node context
                   structlog.contextvars.clear_contextvars()
           
           return wrapper
       return decorator
   ```

2. **Update tool execution logging**
   ```python
   async def _execute_single_tool(self, tool_call: dict, state: dict):
       logger.info("tool_execution_start",
           tool_name=tool_name,
           args=tool_call["args"])
       
       try:
           result = await tool_func.ainvoke(tool_call["args"])
           
           logger.info("tool_execution_complete",
               tool_name=tool_name,
               result_type=type(result).__name__)
           
           # ... rest of the code
       except Exception as e:
           logger.error("tool_execution_failed",
               tool_name=tool_name,
               error=str(e),
               exc_info=True)
   ```

#### 3.2: Individual Agent Nodes

**Files:**
- `services/langgraph/src/nodes/zavhoz.py`
- `services/langgraph/src/nodes/developer.py`
- `services/langgraph/src/nodes/architect.py`
- `services/langgraph/src/nodes/devops.py`
- `services/langgraph/src/nodes/product_owner.py`

**Pattern for each node:**

```python
import structlog
from .base import log_node_execution

logger = structlog.get_logger()

@log_node_execution("zavhoz")  # or "developer", "architect", etc.
async def zavhoz_node(state: dict) -> dict:
    # Node-specific logging
    logger.info("allocating_resources",
        resource_types=["server", "port"])
    
    # ... node logic ...
    
    logger.info("resources_allocated",
        allocated_count=len(allocated))
    
    return {"messages": [...]}
```

**Developer Node specific logging:**
```python
logger.info("spawning_worker",
    repo_name=repo_name,
    worker_type="factory_ai",
    request_id=request_id)

logger.info("worker_result_received",
    request_id=request_id,
    success=result.get("success"),
    duration_sec=result.get("duration"))
```

**Architect Node specific logging:**
```python
logger.info("creating_repository",
    repo_name=repo_full_name,
    complexity=state["project_complexity"],
    is_private=True)

logger.info("repository_created",
    repo_url=repo_info["html_url"],
    clone_url=repo_info["clone_url"])
```

**DevOps Node specific logging:**
```python
logger.info("deployment_start",
    server_ip=target_server_ip,
    port=target_port,
    repo=repo_full_name)

logger.info("ansible_playbook_execution",
    playbook="site.yml",
    inventory="prod",
    server_handle=server_handle)

logger.info("deployment_complete",
    deployed_url=deployed_url,
    duration_sec=round(duration, 2))
```

**Zavhoz Node specific logging:**
```python
logger.info("checking_incidents",
    active_count=len(incidents))

logger.info("resource_allocation_decision",
    needs_server=needs_server,
    needs_port=needs_port,
    available_ports=available_count)
```

#### 3.3: Provisioner Modules

**Files:**
- `services/langgraph/src/provisioner/node.py`
- `services/langgraph/src/provisioner/ansible_runner.py`
- `services/langgraph/src/provisioner/ssh.py`
- `services/langgraph/src/provisioner/recovery.py`

**Key events to log:**

```python
# ansible_runner.py
logger.info("ansible_playbook_start",
    playbook=playbook_name,
    server_handle=server_handle,
    auth_mode=auth_mode)

logger.info("ansible_playbook_complete",
    playbook=playbook_name,
    exit_code=process.returncode,
    duration_sec=duration)

# ssh.py
logger.info("ssh_connection_test",
    host=server_ip,
    success=success)

# recovery.py
logger.info("incident_recovery_start",
    server_handle=server_handle,
    services_count=len(services))

logger.info("service_redeployment",
    service_name=service_name,
    server=server_handle,
    status="success" or "failed")
```

#### Deliverables

- ✅ All nodes use `@log_node_execution` decorator
- ✅ Node context (node name, thread_id) in all logs
- ✅ Tool executions logged with args and results
- ✅ Provisioner operations logged with details

#### Acceptance Criteria

1. Filter logs by node: `docker compose logs langgraph | jq 'select(.node=="developer")'`
2. See full execution flow for a thread_id
3. All tool calls visible in logs
4. Ansible playbook executions tracked with duration

---

### Phase 4: Background Services (Day 6)

**Goal:** Migrate remaining services to structured logging

#### 4.1: Scheduler Service

**Files:**
- `services/scheduler/src/main.py`
- `services/scheduler/src/tasks/*.py`

**Changes:**

```python
# main.py
from shared.logging_config import setup_logging
setup_logging(service_name="scheduler")

# tasks/github_sync.py
logger.info("github_sync_start")

repos_synced = 0
for repo in repos:
    # sync logic
    repos_synced += 1

duration = time.time() - start
logger.info("github_sync_complete",
    repos_synced=repos_synced,
    duration_sec=round(duration, 2))

# tasks/server_sync.py
logger.info("server_sync_start")

logger.info("server_discovered",
    server_handle=handle,
    status=status)

logger.info("server_sync_complete",
    servers_discovered=new_count,
    servers_updated=updated_count,
    duration_sec=round(duration, 2))

# tasks/health_checker.py
logger.info("health_check_start",
    servers_count=len(servers))

logger.info("server_health_check",
    server_handle=handle,
    status="healthy" or "unhealthy",
    response_time_ms=response_time)

logger.info("health_check_complete",
    healthy_count=healthy,
    unhealthy_count=unhealthy,
    duration_sec=round(duration, 2))
```

#### 4.2: Telegram Bot Service

**Files:**
- `services/telegram_bot/src/main.py`

**Changes:**

```python
from shared.logging_config import setup_logging
import uuid

setup_logging(service_name="telegram_bot")

async def handle_message(update: Update, context):
    correlation_id = f"msg_{update.message.message_id}_{int(time.time())}"
    
    logger.info("message_received",
        user_id=user_id,
        chat_id=chat_id,
        message_id=update.message.message_id,
        correlation_id=correlation_id,
        username=update.effective_user.username)
    
    # Publish to Redis with correlation_id
    await redis_client.publish(stream, {
        "correlation_id": correlation_id,
        "user_id": user_id,
        "chat_id": chat_id,
        "text": text,
        ...
    })
    
    logger.info("message_published",
        correlation_id=correlation_id)

# Outgoing messages
logger.info("sending_message",
    chat_id=chat_id,
    correlation_id=data.get("correlation_id"))

logger.info("message_sent",
    chat_id=chat_id,
    correlation_id=data.get("correlation_id"),
    message_id=result.message_id)
```

#### 4.3: Worker Spawner Service

**Files:**
- `services/worker-spawner/src/main.py`

**Changes:**

```python
logger.info("spawn_request_received",
    request_id=request.request_id,
    repo=request.repo,
    branch=request.branch)

logger.info("docker_container_creating",
    request_id=request.request_id,
    image=image_name)

logger.info("docker_container_created",
    request_id=request.request_id,
    container_id=container.id[:12])

logger.info("docker_container_running",
    request_id=request.request_id,
    container_id=container.id[:12])

logger.info("worker_execution_complete",
    request_id=request.request_id,
    exit_code=exit_code,
    duration_sec=round(duration, 2),
    success=exit_code == 0)

logger.info("spawn_result_published",
    request_id=request.request_id,
    channel=result_channel)
```

#### Deliverables

- ✅ Scheduler tasks with metrics (items processed, duration)
- ✅ Telegram bot with correlation_id propagation
- ✅ Worker spawner with container lifecycle tracking

#### Acceptance Criteria

1. Scheduler logs show task execution metrics
2. Telegram bot logs include correlation_id
3. Worker spawner logs show full container lifecycle
4. Can trace message from Telegram → LangGraph → Worker Spawner by correlation_id

---

### Phase 5: Testing & Documentation (Day 7)

**Goal:** Verify implementation and create documentation

#### 5.1: Integration Testing

**Test Scenarios:**

1. **End-to-end message flow**
   ```bash
   # Send Telegram message
   # Expected: See correlation_id in all services
   docker compose logs | grep "msg_123_1735167345"
   ```

2. **Node execution tracking**
   ```bash
   # Trigger graph execution
   # Expected: See node_start and node_complete for each node
   docker compose logs langgraph | jq 'select(.event=="node_start" or .event=="node_complete")'
   ```

3. **Error logging**
   ```bash
   # Trigger error (e.g., invalid server handle)
   # Expected: See structured error with exc_info
   docker compose logs | jq 'select(.level=="error")'
   ```

4. **Performance tracking**
   ```bash
   # Check duration tracking
   docker compose logs | jq 'select(.duration_ms or .duration_sec)'
   ```

#### 5.2: Grafana Loki Setup (Optional)

**Docker Compose additions:**

```yaml
# docker-compose.yml
services:
  loki:
    image: grafana/loki:latest
    ports:
      - "3100:3100"
    volumes:
      - ./loki-config.yaml:/etc/loki/local-config.yaml
    command: -config.file=/etc/loki/local-config.yaml

  promtail:
    image: grafana/promtail:latest
    volumes:
      - /var/lib/docker/containers:/var/lib/docker/containers:ro
      - ./promtail-config.yaml:/etc/promtail/config.yaml
    command: -config.file=/etc/promtail/config.yaml

  grafana:
    image: grafana/grafana:latest
    ports:
      - "3000:3000"
    environment:
      - GF_AUTH_ANONYMOUS_ENABLED=true
      - GF_AUTH_ANONYMOUS_ORG_ROLE=Admin
```

**Example Queries:**

```logql
# All logs from LangGraph
{service="langgraph"}

# Specific node
{service="langgraph", node="developer"}

# Trace request
{correlation_id="msg_456_1735167345"}

# Errors in last hour
{level="error"} |~ ".*" | json

# Slow operations
{duration_ms > 1000} | json

# Developer node duration (avg)
avg_over_time({node="developer"} | json | unwrap duration_ms [5m])
```

#### 5.3: Documentation

**Create/Update docs:**

1. **docs/LOGGING.md** (New)
   - How to use structured logging
   - Available log events reference
   - Grafana query examples
   - Troubleshooting guide

2. **README.md** (Update)
   - Add logging section
   - Environment variables for logging

3. **ARCHITECTURE.md** (Update)
   - Update logging section
   - Add diagram of log flow

**Example logging guide:**

```markdown
# Logging Guide

## How to Add Logs

```python
import structlog

logger = structlog.get_logger()

# Info log
logger.info("event_name", 
    key1=value1, 
    key2=value2)

# Error log
logger.error("error_event",
    error=str(e),
    error_type=type(e).__name__,
    exc_info=True)
```

## Standard Events

### API Events
- `http_request` - HTTP request completed
- `creating_project` - Project creation started
- `project_created` - Project created successfully

### LangGraph Events
- `node_start` - Node execution started
- `node_complete` - Node execution completed
- `tool_execution_start` - Tool call started

### Provisioner Events
- `ansible_playbook_start` - Ansible playbook execution started
- `deployment_complete` - Deployment finished
```

#### Deliverables

- ✅ All integration tests passing
- ✅ Grafana Loki configured (optional)
- ✅ Documentation complete
- ✅ Example queries documented

#### Acceptance Criteria

1. Can trace any request by correlation_id
2. All nodes show start/complete events
3. Error logs include stack traces
4. Documentation has examples for all common scenarios
5. Team can query logs in Grafana

---

## Rollout Strategy

### Development → Staging → Production

#### Development (Days 1-7)
- Implement in feature branch
- Test locally with Docker Compose
- Merge to main after each phase

#### Staging (Week 2)
- Deploy to staging environment
- Monitor log volume and performance
- Adjust log levels if needed
- Test Grafana queries

#### Production (Week 3)
- Deploy with `LOG_LEVEL=INFO`
- Monitor for 2-3 days
- Gradually enable DEBUG for specific services if needed

### Rollback Plan

If issues occur:
1. Set `LOG_FORMAT=console` (fallback to original format)
2. Revert specific service to old logging
3. Full rollback: revert commit, redeploy

**Backward Compatibility:**
- All old `logger.info(f"...")` still works
- `structlog` is compatible with Python's `logging`
- No breaking changes to service behavior

---

## Metrics for Success

### Observability

| Metric | Target | Measurement |
|--------|--------|-------------|
| Log coverage | 90%+ of operations | Manual review |
| Correlation tracking | 100% HTTP requests | Query Grafana |
| Error context | All errors have exc_info | Query `level=error` |
| Node visibility | All nodes have start/end | Count node events |

### Performance

| Metric | Target | Measurement |
|--------|--------|-------------|
| Logging overhead | <5% CPU increase | `docker stats` |
| Log volume | <100MB/day per service | Disk usage |
| JSON parse time | <1ms per entry | Grafana queries |

### Developer Experience

| Metric | Target | Measurement |
|--------|--------|-------------|
| Time to debug | 50% reduction | Team feedback |
| Ease of filtering | Easy to find logs | Team feedback |
| Documentation clarity | All events documented | Doc review |

---

## Open Questions & Future Work

### Answered
- ✅ **Logging library:** structlog (flexible, production-ready)
- ✅ **Output format:** JSON for production, console for dev
- ✅ **Context propagation:** correlation_id + contextvars
- ✅ **Node tracking:** Decorator pattern with context injection

### To Decide Later
- **Log retention:** How long to keep logs? (Suggest: 30 days)
- **Sensitive data filtering:** Auto-redact secrets in logs?
- **Log sampling:** Sample high-volume logs in production?
- **Centralized vs local:** Keep Docker logs or centralize to Loki?

### Future Enhancements
- **Phase 6:** OpenTelemetry integration (see backlog)
- **Phase 7:** Prometheus metrics from logs
- **Phase 8:** Automatic alerting on error patterns
- **Phase 9:** Log-based SLOs (error rate, latency percentiles)

---

## Dependencies

### New Packages

```toml
# shared/pyproject.toml
[project]
dependencies = [
    "structlog>=24.0.0",
    "python-json-logger>=2.0.7",  # Optional fallback
]
```

### Service Updates

Each service needs to import shared logging:

```toml
# services/{service}/pyproject.toml
[project]
dependencies = [
    "shared @ file:///app/shared",  # Already exists in most services
]
```

### Environment Variables

```bash
# .env (defaults)
LOG_LEVEL=INFO
LOG_FORMAT=console

# docker-compose.yml (production overrides)
environment:
  LOG_LEVEL: ${LOG_LEVEL:-INFO}
  LOG_FORMAT: ${LOG_FORMAT:-json}
  SERVICE_NAME: langgraph  # Auto-set per service
```

---

## Risk Assessment

| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|------------|
| Performance degradation | Medium | Low | Benchmark first, use async logging |
| Breaking existing logs | High | Low | Backward compatible, gradual migration |
| Too much log volume | Medium | Medium | Start with INFO level, adjust based on monitoring |
| Developer resistance | Low | Medium | Good docs, clear examples, gradual rollout |
| Incomplete migration | Medium | Medium | Checklist per service, code review |
| Secrets in logs | High | Low | Code review, add tests for PII detection |

---

## Team Coordination

### Required Reviews
- [ ] Architecture review (logging structure)
- [ ] Security review (ensure no secrets in logs)
- [ ] Performance review (log volume acceptable)
- [ ] Documentation review (clear for team)

### Communication Plan
1. **Kickoff:** Share this plan, get feedback
2. **Daily updates:** Progress on each phase
3. **Demo:** Show Grafana queries after Phase 5
4. **Retrospective:** After production deployment

---

## Conclusion

Structured logging will significantly improve observability and debugging capabilities. The phased approach allows for safe, gradual migration with minimal risk.

**Next Steps:**
1. Review and approve this plan
2. Start Phase 1 (Infrastructure Setup)
3. Test with one service before full rollout

**Estimated Timeline:**
- Days 1-7: Implementation
- Week 2: Staging validation
- Week 3: Production deployment

**Success Criteria:**
- ✅ All services log in JSON format
- ✅ Can trace requests via correlation_id
- ✅ All LangGraph nodes visible in logs
- ✅ Team can easily debug issues via Grafana

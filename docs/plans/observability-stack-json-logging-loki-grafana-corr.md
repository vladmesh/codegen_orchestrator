# Observability stack: JSON logging + Loki + Grafana + correlation propagation

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Admin dashboard needs log viewing. Current state: all services use shared/log_config (structlog),
JSON renderer exists but LOG_FORMAT=json not set in docker-compose. correlation_id already exists
in BaseMessage (shared/contracts/base.py) — all queue DTOs auto-include it. Helpers
set_correlation_id/get_correlation_id exist in shared/log_config/correlation.py but are not called
in consumers. No log aggregation — only docker logs.

Goal: Grafana + Loki + Promtail stack with correlation ID propagation across all services.

## Steps

1. [ ] Enable JSON logging in docker-compose
   - **Input**: docker-compose.yml, .env.example
   - **Output**: All services have LOG_FORMAT=json and SERVICE_NAME set in docker-compose.yml environment. .env.example updated with LOG_FORMAT=console (dev override).
   - **Test**: `make up && docker compose logs api --tail 5` — output is valid JSON with "service", "event", "timestamp" fields

2. [ ] Add Loki + Promtail + Grafana to docker-compose
   - **Input**: docker-compose.yml, infra/ directory
   - **Output**: Three new services in docker-compose.yml: loki (port 3100), grafana (port 3000), promtail. New config files: infra/loki.yml, infra/promtail.yml, infra/grafana/datasources.yml. Promtail scrapes /var/lib/docker/containers (read-only). Grafana auto-provisions Loki datasource. New volumes: loki-data, grafana-data.
   - **Test**: `make up && curl -s http://localhost:3100/ready` returns "ready". `curl -s http://localhost:3000/api/health` returns ok. Grafana Explore tab shows logs from all services.

3. [ ] Bind correlation_id in all Redis stream consumers
   - **Input**: shared/contracts/base.py (already has correlation_id), shared/log_config/correlation.py (has set_correlation_id), all consumers: services/langgraph/src/consumers/{engineering,deploy,po,_base}.py, services/scheduler/src/tasks/*.py, services/scaffolder/src/consumer.py, services/worker-manager consumer, services/telegram_bot stream listener
   - **Output**: Every consumer extracts correlation_id from message data and calls set_correlation_id() before processing. For BaseMessage consumers: extract from msg.data["correlation_id"]. For PO flat-field consumers: extract from flat fields. Clear context after processing (clear_context() or unbind).
   - **Test**: Unit test per consumer — mock structlog.contextvars.bind_contextvars, verify correlation_id is bound when message is processed. Test file: services/langgraph/tests/unit/test_correlation_binding.py

4. [ ] Pass X-Correlation-ID in API calls from consumers
   - **Input**: services/langgraph/src/clients/*.py (API client classes), services/scheduler/src/tasks/*.py (httpx calls to API)
   - **Output**: All httpx/API client calls include X-Correlation-ID header from get_correlation_id(). API correlation middleware already binds it on receipt (services/api/src/main.py:64-67).
   - **Test**: Unit test — mock httpx, verify X-Correlation-ID header is present in outgoing requests. Integration: publish message with known correlation_id → verify it appears in API access logs.

5. [ ] Pre-build Grafana dashboard
   - **Input**: infra/grafana/ directory
   - **Output**: infra/grafana/dashboards/service-logs.json — provisioned dashboard with panels: log stream by service (multi-select), filter by level, correlation_id search box, error rate over time. infra/grafana/dashboards.yml — dashboard provisioning config.
   - **Test**: Open Grafana → Dashboards → "Service Logs" exists with working panels. Filter by service=api shows only API logs.

6. [ ] Update docs/LOGGING.md
   - **Input**: docs/LOGGING.md (existing), implementation results from steps 1-5
   - **Output**: Updated architecture section reflecting actual infra (not TODO). Remove "Gaps" that are now resolved. Add Grafana access instructions (port 3000, default creds). Add correlation_id usage examples for new consumers.
   - **Test**: Manual review — doc matches reality



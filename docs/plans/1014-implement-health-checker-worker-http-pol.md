# #1014 Implement health_checker worker (HTTP polling + auto-incidents + alerts)

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Phase 1 of Server Health Monitoring (brainstorm bs-69482380). Siblings #1011 (provisioning), #1012 (parser), #1013 (DB model) are done. This task fills in the `health_checker.py` skeleton with actual HTTP polling, DB updates, incident auto-creation, and Telegram alerts.

**Current state**:
- `health_checker.py` is an empty `while True: sleep(60)` loop
- node_exporter (:9100) and cadvisor (:8080) are installed on managed servers (UFW-restricted)
- Prometheus text parser exists: `parse_node_exporter()` → `NodeMetrics`, `parse_cadvisor()` → `list[ContainerMetrics]`
- Server model has health fields: `cpu_usage_pct`, `load_avg_*`, `network_*_errors`, `container_count_*`, `uptime_seconds`, `last_health_check`
- `server_metrics_history` table exists with JSON `metrics` column
- API endpoints exist: `POST /api/incidents/`, `POST /api/servers/{handle}/metrics-history`, `PATCH /api/servers/{handle}`
- Scheduler API client lacks `create_incident()` and `create_metrics_history()` methods
- `notify_admins()` exists in `shared/notifications.py`
- Two active servers: `vps-265601` (active), `vps-267180` (active)

## Steps

1. [ ] Extend SchedulerAPIClient with health-checker methods
   - **Input**: `services/scheduler/src/clients/api.py`
   - **Output**: Three new methods: `create_incident(server_handle, incident_type, details, affected_services)`, `create_metrics_history(server_handle, metrics)`, `get_active_incidents(server_handle, incident_type)` (for dedup)
   - **Test**: Unit test mocking httpx — verify correct paths, payloads, and return types

2. [ ] Implement core health check loop (HTTP polling + metric extraction)
   - **Input**: `services/scheduler/src/tasks/health_checker.py`, `services/scheduler/src/metrics/` (parser API)
   - **Output**: `health_check_worker()` iterates managed+active servers, HTTP GETs `:9100/metrics` and `:8080/metrics`, parses via `parse_node_exporter()` / `parse_cadvisor()`, updates Server via `api_client.update_server()` with `ServerUpdate`, appends metrics history via `api_client.create_metrics_history()`. Uses `httpx.AsyncClient` for HTTP fetches with short timeout (10s). Logs per-server results.
   - **Test**: Unit test with mocked API client and mocked HTTP responses — verify ServerUpdate fields populated correctly from parsed metrics, verify history snapshot created

3. [ ] Implement incident auto-creation with dedup
   - **Input**: `services/scheduler/src/tasks/health_checker.py`
   - **Output**: After each server check: (a) if HTTP fetch fails → create `SERVER_UNREACHABLE` incident (if no active incident of same type for this server), (b) if RAM or disk > 90% → create `RESOURCE_EXHAUSTED` incident (with details showing which resource). Check active incidents first to avoid duplicates. On successful check after previous `SERVER_UNREACHABLE` → auto-resolve (PATCH incident).
   - **Test**: Unit test — verify incident created on HTTP failure, verify dedup (no duplicate incidents), verify auto-resolve on recovery

4. [ ] Implement Telegram alerting on incident creation
   - **Input**: `services/scheduler/src/tasks/health_checker.py`, `shared/notifications.py`
   - **Output**: When a new incident is created, call `notify_admins()` with appropriate level (`"critical"` for unreachable, `"warning"` for resource exhaustion). Message includes server handle, IP, incident type, and details (e.g., "RAM at 94%").
   - **Test**: Unit test — verify `notify_admins` called with correct message and level when incident is created, not called on dedup

5. [ ] Implement daily metrics history cleanup
   - **Input**: `services/scheduler/src/tasks/health_checker.py`, API client
   - **Output**: Separate async task or check within loop: once per day, call API endpoint to delete history older than 7 days. Track last cleanup time. If no API endpoint for bulk delete exists, add `delete_old_metrics_history(hours=168)` to API + client.
   - **Test**: Unit test — verify cleanup called approximately daily, verify correct retention period

6. [ ] Integration tests
   - **Input**: All code from steps 1-5
   - **Output**: Integration tests that verify: (a) full health check cycle with real API (mocked HTTP to exporters), (b) incident lifecycle (create → dedup → resolve), (c) metrics history append + cleanup
   - **Test**: `tests/integration/test_health_checker.py` — requires running API + DB


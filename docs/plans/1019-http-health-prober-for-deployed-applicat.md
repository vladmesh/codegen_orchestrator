# #1019 HTTP health prober for deployed applications + SSL expiry check

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Phase 2 of Server & Application Health Monitoring brainstorm (bs-69482380). Phase 1 done: server health polling, metrics history, admin dashboard. #1016 done: data layer (Application health fields, ApplicationHealthHistory table, API endpoints) + admin UI with charts. This task builds the **worker** that populates that data.

**Current state**: `health_checker.py` polls servers via node_exporter/cadvisor. Applications have `response_time_ms`, `ssl_expires_at`, `uptime_pct_24h`, `last_health_check` fields (all null). `ApplicationHealthHistory` table exists with GET/POST/DELETE endpoints. `check_http_health()` utility exists in `shared/clients/infra_client.py`. `IncidentType` has `SERVICE_DOWN` but not `SSL_EXPIRING`.

**Key design decisions**:
- Health probe URL: `http://{server.public_ip}:{port}/health` (no domain field exists — use IP + port from allocations)
- Consecutive failure tracking: in-memory dict `{app_id: fail_count}` — create `SERVICE_DOWN` incident after 3 consecutive fails
- SSL check: direct socket connection to `{server.public_ip}:{port}` to extract cert expiry
- Uptime calculation: count healthy entries / total entries in last 24h from health history
- Add to existing `health_check_worker` loop: after server checks, iterate deployed applications

## Steps

1. [ ] Add SSL_EXPIRING to IncidentType enum ⚠️ needs-approval
   - **Input**: `shared/models/incident.py`
   - **Output**: New enum value `SSL_EXPIRING = "ssl_expiring"` in `IncidentType`
   - **Test**: Unit test verifying enum has 5 values including SSL_EXPIRING

2. [ ] Add application API methods to SchedulerAPIClient
   - **Input**: `services/scheduler/src/clients/api.py`
   - **Output**: New methods: `get_applications(server_handle=None, status=None)` → list[dict], `update_application(app_id, fields)` → dict, `create_app_health_history(app_id, metrics)` → dict, `delete_old_app_health_history(retention_hours)` → dict
   - **Test**: Unit tests for each new method (mock httpx)

3. [ ] Implement SSL certificate expiry checker
   - **Input**: New file `services/scheduler/src/tasks/ssl_checker.py`
   - **Output**: `async def check_ssl_expiry(host: str, port: int) -> datetime | None` — connects via ssl+socket, returns cert notAfter as datetime, or None on failure. Runs in executor (blocking socket call).
   - **Test**: Unit tests: mock ssl context → valid cert returns datetime, expired cert returns past datetime, connection failure returns None

4. [ ] Implement application health prober
   - **Input**: New file `services/scheduler/src/tasks/app_health_prober.py`, uses `check_http_health` from `shared/clients/infra_client`, `check_ssl_expiry` from step 3, API client methods from step 2
   - **Output**: `async def check_application(app, server_ip, consecutive_failures)` — probes `http://{server_ip}:{port}/health`, checks SSL on same host:port, updates Application fields via API (status, response_time_ms, ssl_expires_at, last_health_check), appends health history snapshot, returns updated consecutive failures count. `async def app_health_probe_cycle(api_client)` — fetches all deployed apps grouped by server, iterates, handles incident creation/resolution (SERVICE_DOWN after 3 fails, SSL_EXPIRING 7 days before expiry), computes uptime_pct_24h from history.
   - **Test**: Unit tests: healthy app updates status+response_time, unhealthy app increments fail counter, 3 consecutive fails creates SERVICE_DOWN incident, SSL expiry < 7 days creates SSL_EXPIRING incident, recovery auto-resolves incidents

5. [ ] Integrate app prober into health_check_worker loop
   - **Input**: `services/scheduler/src/tasks/health_checker.py`, `services/scheduler/src/main.py`
   - **Output**: After server check loop completes, call `app_health_probe_cycle(api_client)`. Add app health history cleanup to existing daily cleanup job. No new worker process — runs in same loop.
   - **Test**: Unit test: health_check_worker calls app_health_probe_cycle after server checks

6. [ ] Integration test for full health probe flow
   - **Input**: All components from steps 1-5
   - **Output**: Integration test: create server + app + port allocation → run app_health_probe_cycle with mocked HTTP → verify Application fields updated, health history created, incident handling works
   - **Test**: `make test-scheduler-integration` passes


# #1016 Admin UI: application health status and response times

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Phase 2 of the Server & Application Health Monitoring brainstorm (bs-69482380). Phase 1 tasks (#1011–#1015) are done: servers have node_exporter + cadvisor polling, metrics history, and an admin dashboard with charts. Now we need to display application-level health in the admin UI.

**Current state**: Applications are shown as a simple table inside the server Overview tab (ServersPage.tsx:78-132) with columns: name, ports, status, last check. No response time, no SSL info, no uptime %, no charts, no history table.

**Dependency**: #1019 (HTTP health prober) will populate the data. This task builds the data layer + UI so the prober has somewhere to write and the admin can see results. The UI will show "No health data" gracefully until #1019 is implemented.

## Steps

1. [ ] Extend Application model + migration
   - **Input**: `shared/models/application.py`, `services/api/src/schemas/application.py`
   - **Output**: New fields on Application: `response_time_ms` (Integer, nullable), `ssl_expires_at` (DateTime, nullable), `uptime_pct_24h` (Float, nullable). Update `ApplicationUpdate` schema to accept these new fields. Migration file.
   - **Test**: Unit test — create Application with new fields, verify persistence and nullable defaults

2. [ ] Create ApplicationHealthHistory model + migration ⚠️ needs-approval
   - **Input**: `shared/models/server_metrics_history.py` (reference pattern), `shared/models/__init__.py`
   - **Output**: New model `ApplicationHealthHistory` in `shared/models/application_health_history.py`: `id` (BigInteger PK), `application_id` (FK to applications.id), `recorded_at` (DateTime, server_default=now), `metrics` (JSON — stores response_time_ms, status_code, ssl_days_remaining, healthy bool). Composite index on (application_id, recorded_at). Register in `__init__.py`.
   - **Test**: Unit test — create history entry, verify JSON metrics storage and FK constraint

3. [ ] Add API endpoints for application health history
   - **Input**: `services/api/src/routers/applications.py`, `services/api/src/schemas/application.py`, `services/api/src/routers/servers.py` (reference for history endpoints)
   - **Output**: New schemas `ApplicationHealthHistoryCreate` and `ApplicationHealthHistoryRead`. Endpoints: `POST /applications/{id}/health-history` (append snapshot), `GET /applications/{id}/health-history?hours=24` (read time range), `DELETE /applications/health-history?retention_hours=168` (cleanup). Follow same pattern as server metrics history.
   - **Test**: Unit tests for each endpoint — create, read with time filter, cleanup retention

4. [ ] Update frontend types + API client
   - **Input**: `services/admin-frontend/src/types/api.ts`
   - **Output**: Update `Application` interface with `response_time_ms`, `ssl_expires_at`, `uptime_pct_24h`. New `ApplicationHealthEntry` interface (id, application_id, recorded_at, metrics: {response_time_ms, status_code, ssl_days_remaining, healthy}). 
   - **Test**: TypeScript compilation succeeds

5. [ ] Enhance applications table with health columns
   - **Input**: `ServersPage.tsx` — `ServerApplications` component (lines 78-132)
   - **Output**: Add columns to applications table: health status indicator (colored dot based on status), response time (ms), SSL expiry (date + warning color if < 7 days), uptime % (24h with color coding). Add "No health data" empty state when fields are null.
   - **Test**: Visual verification — table renders with new columns, handles null values gracefully

6. [ ] Add expandable application rows with response time chart
   - **Input**: `ServersPage.tsx` — `ChartCard` component (reuse), new `GET /applications/{id}/health-history` endpoint
   - **Output**: Each application row is expandable (click to expand, like ServerRow). Expanded view shows: overview cards (response time, uptime %, SSL status, last check), response time chart (1h/24h toggle using recharts AreaChart). Follow the ChartCard + tab pattern from server rows.
   - **Test**: Visual verification — expand works, chart renders with mock data, time range toggle works

7. [ ] Integration test for health history flow
   - **Input**: All components from steps 1-6
   - **Output**: Integration test: create application → POST health history entries → GET history with time filter → verify API response matches. Test cleanup endpoint removes old entries.
   - **Test**: `make test-api-integration` passes


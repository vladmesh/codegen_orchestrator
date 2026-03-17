# #1013 Extend Server model with health metrics + metrics history table

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Phase 1, step 3 of the Server Health Monitoring initiative (brainstorm bs-69482380).
Siblings #1011 (provisioning) and #1012 (prometheus parser) are done — node_exporter + cadvisor are installed on servers, and `NodeMetrics`/`ContainerMetrics` dataclasses + parser exist in `services/scheduler/src/metrics/`.

This task adds DB storage for the metrics those parsers produce. The next task (#1014, health_checker worker) will use these models to persist polled data.

**Current state**:
- `Server` model has capacity/usage fields from Time4VPS API but NO real metrics (cpu%, load, network errors, container counts, uptime)
- `last_health_check` field exists but is never populated
- No metrics history table exists
- `ServerDTO`, `ServerUpdate`, `ServerRead` schemas do not expose the new fields

## Steps

1. [ ] Add health metric columns to Server model
   - **Input**: `shared/models/server.py`
   - **Output**: New nullable columns: `cpu_usage_pct` (Float), `load_avg_1m/5m/15m` (Float), `network_rx_errors` (BigInteger), `network_tx_errors` (BigInteger), `container_count_running` (Integer), `container_count_total` (Integer), `uptime_seconds` (Float)
   - **Test**: Unit test — instantiate Server with new fields, verify defaults are None

2. [ ] Create ServerMetricsHistory model
   - **Input**: New file `shared/models/server_metrics_history.py`
   - **Output**: Model with columns: `id` (BigInteger PK), `server_handle` (FK → servers.handle, indexed), `recorded_at` (DateTime, indexed, default=utcnow), `metrics` (JSON — full snapshot). Register in `shared/models/__init__.py`
   - **Test**: Unit test — instantiate model, verify fields and table name

3. [ ] Create Alembic migration
   - **Input**: Models from steps 1-2, head=`42e0acc86b20`
   - **Output**: Migration that: (a) ADDs columns to `servers`, (b) CREATEs `server_metrics_history` table with indexes on `(server_handle, recorded_at)`
   - **Test**: `make migrate` succeeds; `make test-integration` for API service passes

4. [ ] Update shared contracts (DTOs)
   - **Input**: `shared/contracts/dto/server.py`
   - **Output**: Add new fields to `ServerDTO` (all Optional, default None). Add new fields to `ServerUpdate` (all Optional). Add `ServerMetricsHistoryDTO` for the history table.
   - **Test**: Unit test — construct DTOs with and without new fields ⚠️ needs-approval (shared/contracts change)

5. [ ] Update API schemas + PATCH endpoint
   - **Input**: `services/api/src/schemas/server.py`, `services/api/src/routers/servers.py`
   - **Output**: Add new fields to `ServerRead`. Add new fields to `allowed_fields` in PATCH handler. Add `last_health_check` to allowed_fields + datetime_fields.
   - **Test**: Unit test — verify ServerRead schema includes new fields; integration test — PATCH server with health metrics, GET and verify round-trip

6. [ ] Add metrics history API endpoints
   - **Input**: `services/api/src/routers/servers.py`
   - **Output**: `GET /{handle}/metrics-history?hours=24` — returns recent history entries. `POST /{handle}/metrics-history` — append a snapshot (internal use by health_checker).
   - **Test**: Integration test — POST a snapshot, GET history, verify content and ordering

7. [ ] Integration test: full round-trip
   - **Input**: All changes from steps 1-6
   - **Output**: Test that: creates server → PATCHes with health metrics → POSTs metrics history → GETs history → verifies all fields
   - **Test**: `make test-api-integration` passes


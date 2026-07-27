# Grafana PostgreSQL dashboards, verification

Date: 2026-07-27

## Provisioning

Grafana now receives a PostgreSQL datasource with UID `postgres`. Its username and password come
from `GRAFANA_DB_USER` and `GRAFANA_DB_PASSWORD`; neither credential is stored in the repository.
`infra/postgres/init-grafana-reader.sh` creates the role without superuser, database-creation,
role-creation, or inheritance privileges, then grants only `CONNECT`, schema usage, and `SELECT`
on current and future public tables.

The init hook runs automatically for a new PostgreSQL volume. On an existing volume, deploy the
updated Compose configuration, then run this once after the `db` container is recreated with the
two Grafana environment variables:

```bash
docker compose exec db /docker-entrypoint-initdb.d/20-grafana-reader.sh
```

The command is idempotent and also rotates the reader password to the configured value. Grafana
depends on the healthy database and uses at most five open connections.

## Dashboards

- `Server capacity` reads `server_metrics_history`: free RAM, free disk, CPU/load, container count,
  and freshness by server.
- `Run operations` reads `runs`, `tasks`, and `task_events`: outcomes by type, failure rate,
  duration, retries per story, tokens, and cost by head profile.
- Token and cost panels return SQL `NULL` if a profile has no effort values. Grafana therefore
  renders no data, never a fabricated zero.

## Verification

`uv run python scripts/verify_grafana_dashboards.py` created the disposable
`grafana_dashboard_verify` database, seeded four runs and two task events, and dropped that
database in `finally`. The fixture covered a completed engineering run, a failed deploy, a retry
of one story, and a completed QA run with `total_tokens` and `cost_usd` both `NULL`. It confirmed:

- four terminal outcomes and a 25% failure rate;
- a repeated story and task iteration 2;
- absent token and cost aggregates remain `NULL`.

The live `orchestrator` database was not seeded. At verification it had 508 genuine
`server_metrics_history` rows for one server and zero `runs` and `task_events` rows. Hardware
panel SQL was executed against those rows successfully. `EXPLAIN (ANALYZE, BUFFERS, TIMING OFF)`
reported 8.262 ms for free memory, 3.631 ms for containers, and 0.443 ms for freshness. The small
history table is scanned sequentially, which is appropriate at 508 rows; the time-filter and
server-history indexes support the filtered and per-server query paths as it grows.

There are no live runs at handoff, so run panels were validated only on the disposable seeded
database. This report does not claim live run-panel verification.

## Automated checks

`uv run pytest tests/unit/test_grafana_dashboards.py -q` passed: 4 tests.

`uv run ruff check tests/unit/test_grafana_dashboards.py scripts/verify_grafana_dashboards.py`
passed.

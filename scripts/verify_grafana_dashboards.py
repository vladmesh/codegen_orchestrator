"""Verify Grafana SQL against isolated seed data and current server metrics.

The run dashboard fixture is created in a disposable PostgreSQL database, never
in the orchestrator database. The server checks stay read-only and use the
current production-like database so the report records real history data.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time

SEED_DATABASE = "grafana_dashboard_verify"
DOCKER = shutil.which("docker")


def _command(container: str, user: str, database: str, sql: str) -> list[str]:
    if not DOCKER:
        raise RuntimeError("docker is not installed")
    return [
        DOCKER,
        "exec",
        "-i",
        container,
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        user,
        "-d",
        database,
        "-At",
        "-c",
        sql,
    ]


def _psql(container: str, user: str, database: str, sql: str) -> str:
    completed = subprocess.run(
        _command(container, user, database, sql), check=True, text=True, capture_output=True
    )
    return completed.stdout.strip()


def _create_seed_schema(container: str, user: str) -> None:
    if not DOCKER:
        raise RuntimeError("docker is not installed")
    subprocess.run(
        [DOCKER, "exec", container, "dropdb", "-U", user, "--if-exists", SEED_DATABASE],
        check=True,
    )
    subprocess.run([DOCKER, "exec", container, "createdb", "-U", user, SEED_DATABASE], check=True)
    _psql(
        container,
        user,
        SEED_DATABASE,
        """
        CREATE TABLE runs (
          id text PRIMARY KEY, type text NOT NULL, status text NOT NULL,
          story_id text, task_id text, iteration integer, created_at timestamptz NOT NULL,
          started_at timestamptz, completed_at timestamptz, total_tokens integer,
          cost_usd double precision, agent_profile json
        );
        CREATE TABLE tasks (id text PRIMARY KEY, story_id text);
        CREATE TABLE task_events (
          id integer PRIMARY KEY, task_id text NOT NULL, iteration integer,
          created_at timestamptz NOT NULL
        );
        INSERT INTO tasks VALUES ('task-retry', 'story-retry');
        INSERT INTO task_events VALUES
          (1, 'task-retry', 1, now() - interval '3 hours'),
          (2, 'task-retry', 2, now() - interval '2 hours');
        INSERT INTO runs VALUES
          ('success', 'engineering', 'completed', 'story-success', 'task-success', 1,
           now() - interval '4 hours', now() - interval '4 hours', now() - interval '3 hours',
           1200, 0.12, '{"name":"codex"}'),
          ('failure', 'deploy', 'failed', 'story-failure', 'task-failure', 1,
           now() - interval '3 hours', now() - interval '3 hours', now() - interval '2 hours',
           800, 0.08, '{"name":"claude"}'),
          ('retry', 'engineering', 'completed', 'story-retry', 'task-retry', 2,
           now() - interval '2 hours', now() - interval '2 hours', now() - interval '90 minutes',
           400, 0.04, '{"name":"codex"}'),
          ('no-effort', 'qa', 'completed', 'story-no-effort', 'task-qa', 1,
           now() - interval '1 hour', now() - interval '1 hour', now() - interval '55 minutes',
           NULL, NULL, NULL);
        """,
    )


def _verify_seeded_runs(container: str, user: str) -> None:
    checks = {
        "outcomes": "SELECT count(*) FROM runs WHERE status IN ('completed', 'failed')",
        "failure_rate": (
            "SELECT 100.0 * count(*) FILTER (WHERE status = 'failed') "
            "/ NULLIF(count(*), 0) FROM runs"
        ),
        "duration": (
            "SELECT avg(EXTRACT(EPOCH FROM completed_at - started_at)) FROM runs "
            "WHERE completed_at IS NOT NULL"
        ),
        "retries": "SELECT count(*) FROM runs WHERE story_id = 'story-retry'",
        "task_events": "SELECT max(iteration) FROM task_events WHERE task_id = 'task-retry'",
        "tokens_without_zero_fallback": (
            "SELECT sum(total_tokens) IS NULL FROM runs WHERE id = 'no-effort'"
        ),
        "cost_without_zero_fallback": (
            "SELECT sum(cost_usd) IS NULL FROM runs WHERE id = 'no-effort'"
        ),
    }
    results = {name: _psql(container, user, SEED_DATABASE, sql) for name, sql in checks.items()}
    expected = {
        "outcomes": "4",
        "retries": "1",
        "task_events": "2",
        "tokens_without_zero_fallback": "t",
        "cost_without_zero_fallback": "t",
    }
    if any(results[name] != value for name, value in expected.items()):
        raise RuntimeError(f"Unexpected seeded run results: {results}")
    print("seeded run dashboard checks:")
    for name, result in results.items():
        print(f"  {name}: {result}")


def _verify_live_server_metrics(container: str, user: str, database: str) -> None:
    checks = {
        "metrics_rows": "SELECT count(*) FROM server_metrics_history",
        "free_memory": (
            "SELECT count(*) FROM (SELECT ((metrics->>'ram_total_bytes')::numeric - "
            "(metrics->>'ram_used_bytes')::numeric) / 1024 / 1024 FROM "
            "server_metrics_history WHERE recorded_at >= now() - interval '24 hours') q"
        ),
        "free_disk": (
            "SELECT count(*) FROM (SELECT ((metrics->>'disk_total_bytes')::numeric - "
            "(metrics->>'disk_used_bytes')::numeric) / 1024 / 1024 FROM "
            "server_metrics_history WHERE recorded_at >= now() - interval '24 hours') q"
        ),
        "cpu_load": (
            "SELECT count(*) FROM (SELECT (metrics->>'cpu_usage_pct')::numeric, "
            "(metrics->>'load_avg_1m')::numeric FROM server_metrics_history WHERE "
            "recorded_at >= now() - interval '24 hours') q"
        ),
        "containers": (
            "SELECT count(*) FROM (SELECT json_array_length(COALESCE(metrics->'containers', "
            "'[]'::json)) FROM server_metrics_history WHERE recorded_at >= now() - "
            "interval '24 hours') q"
        ),
        "freshness": (
            "SELECT count(*) FROM (SELECT server_handle, EXTRACT(EPOCH FROM now() - "
            "max(recorded_at)) FROM server_metrics_history GROUP BY server_handle) q"
        ),
    }
    print("live server dashboard checks:")
    for name, sql in checks.items():
        started = time.monotonic()
        result = _psql(container, user, database, sql)
        elapsed_ms = (time.monotonic() - started) * 1000
        print(f"  {name}: {result} rows, {elapsed_ms:.1f} ms")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", default="codegen_orchestrator-db-1")
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--database", default="orchestrator")
    args = parser.parse_args()

    _create_seed_schema(args.container, args.user)
    try:
        _verify_seeded_runs(args.container, args.user)
        _verify_live_server_metrics(args.container, args.user, args.database)
    finally:
        if not DOCKER:
            raise RuntimeError("docker is not installed")
        subprocess.run(
            [
                DOCKER,
                "exec",
                args.container,
                "dropdb",
                "-U",
                args.user,
                "--if-exists",
                SEED_DATABASE,
            ],
            check=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

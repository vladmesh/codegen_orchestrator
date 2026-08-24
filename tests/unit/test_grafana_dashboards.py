"""Static contracts for provisioned operational Grafana dashboards."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def _dashboard(name: str) -> dict:
    return json.loads((ROOT / "infra" / "grafana" / "dashboards" / name).read_text())


def _queries(dashboard: dict) -> str:
    return "\n".join(
        target.get("rawSql", "")
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
    )


def test_postgres_datasource_uses_dedicated_environment_credentials() -> None:
    config = yaml.safe_load((ROOT / "infra" / "grafana" / "datasources.yml").read_text())
    postgres = next(source for source in config["datasources"] if source["uid"] == "postgres")

    assert postgres["type"] == "postgres"
    assert postgres["user"] == "$GRAFANA_DB_USER"
    assert "$GRAFANA_DB_PASSWORD" in postgres["secureJsonData"].values()
    assert postgres["database"] == "$POSTGRES_DB"


def test_grafana_reader_init_is_limited_to_select_privileges() -> None:
    script = (ROOT / "infra" / "postgres" / "init-grafana-reader.sh").read_text()

    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT" in script
    assert "GRANT SELECT ON ALL TABLES" in script
    assert "ALTER DEFAULT PRIVILEGES" in script
    assert "GRANT INSERT" not in script
    assert "GRANT UPDATE" not in script
    assert "GRANT DELETE" not in script


def test_servers_dashboard_covers_current_history_metrics() -> None:
    dashboard = _dashboard("server-capacity.json")
    titles = {panel["title"] for panel in dashboard["panels"]}
    queries = _queries(dashboard)

    assert {"Free memory", "Free disk", "CPU and load", "Containers", "Metrics freshness"} <= titles
    assert "server_metrics_history" in queries
    assert "ram_total_bytes" in queries
    assert "disk_total_bytes" in queries
    assert "containers" in queries
    assert "json_array_length" in queries


def test_runs_dashboard_exposes_outcomes_retries_and_effort_without_zero_fallbacks() -> None:
    dashboard = _dashboard("run-operations.json")
    titles = {panel["title"] for panel in dashboard["panels"]}
    queries = _queries(dashboard)

    assert {
        "Runs by type and outcome",
        "Failure rate",
        "Run duration",
        "Story retries",
        "Tokens by head profile",
        "Cost by head profile",
    } <= titles
    assert "runs" in queries
    assert "task_events" in queries
    assert "engineering_attempt_ledger" in queries
    assert "cost_microusd" in queries
    assert "agent_profile" in queries
    assert "NULLIF" in queries
    assert "COALESCE(total_tokens, 0)" not in queries
    assert "COALESCE(cost_usd, 0)" not in queries

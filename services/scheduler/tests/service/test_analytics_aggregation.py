"""Service test: real Loki → aggregator → analytics_hourly rows in the DB.

Covers the path the LK depends on: logs pushed to Loki are queried by the
aggregator, turned into metrics and upserted through the API. Runs against a
real Loki and a real API/DB (docker/test/service/scheduler.yml).
"""

import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
import uuid

import httpx
import pytest

from shared.analytics_health import (
    ANALYTICS_HEARTBEAT_KEY,
    CollectionState,
    collection_state,
    decode_heartbeat,
)
from shared.clients.loki import LokiClient
from src.tasks import analytics_aggregator

LOKI_URL = os.environ["LOKI_URL"]
INTERNAL_KEY = os.environ["INTERNAL_API_KEY"]

SERVICE_NAME = "backend"


async def _push_logs(project_id: str, service_name: str, entries: list[tuple[datetime, dict]]):
    """Push JSON log lines into Loki under the labels the aggregator queries."""
    payload = {
        "streams": [
            {
                "stream": {
                    "job": "docker",
                    "project_id": project_id,
                    "compose_service": service_name,
                },
                "values": [
                    [str(int(ts.timestamp() * 1_000_000_000)), json.dumps(line)]
                    for ts, line in entries
                ],
            }
        ]
    }
    async with httpx.AsyncClient(base_url=LOKI_URL, timeout=30.0) as client:
        resp = await client.post("/loki/api/v1/push", json=payload)
        assert resp.status_code == httpx.codes.NO_CONTENT, resp.text


async def _wait_for_logs(
    loki: LokiClient,
    project_id: str,
    start: datetime,
    end: datetime,
    expected: int,
):
    """Loki ingestion is asynchronous — poll until the pushed lines are queryable."""
    query = f'{{job="docker", project_id="{project_id}"}} | json'
    for _ in range(60):
        logs = await loki.query_range(query, start, end)
        if len(logs) >= expected:
            return logs
        await asyncio.sleep(1)
    raise AssertionError(f"Loki never returned {expected} entries for project {project_id}")


@pytest.fixture
async def running_application(api_client):
    """A project with a repository and a running application, as the API sees it."""
    project_id = str(uuid.uuid4())
    handle = f"analytics-srv-{uuid.uuid4().hex[:8]}"

    async with httpx.AsyncClient(
        base_url=api_client.base_url,
        timeout=30.0,
        headers={"X-Internal-Key": INTERNAL_KEY},
    ) as client:
        telegram_id = 700_000_000 + uuid.uuid4().int % 1_000_000
        resp = await client.post(
            "/api/users/",
            json={"telegram_id": telegram_id, "username": f"analytics-{telegram_id}"},
        )
        assert resp.status_code == httpx.codes.CREATED, resp.text

        resp = await client.post(
            "/api/projects/",
            json={
                "initiating_run_id": "test-run-1",
                "id": project_id,
                "title": f"Analytics {project_id[:8]}",
                "status": "active",
            },
            headers={"X-Telegram-ID": str(telegram_id)},
        )
        assert resp.status_code == httpx.codes.CREATED, resp.text

        resp = await client.post(
            "/api/servers/",
            json={"handle": handle, "host": f"{handle}.example.com", "public_ip": "10.9.9.9"},
        )
        assert resp.status_code == httpx.codes.CREATED, resp.text

        resp = await client.post(
            "/api/repositories/",
            json={
                "project_id": project_id,
                "name": f"repo-{project_id[:8]}",
                "git_url": f"https://github.com/test-org/repo-{project_id[:8]}.git",
            },
        )
        assert resp.status_code == httpx.codes.CREATED, resp.text
        repo_id = resp.json()["id"]

        resp = await client.post(
            "/api/applications/",
            json={
                "repo_id": repo_id,
                "server_handle": handle,
                "service_name": SERVICE_NAME,
                "status": "running",
            },
        )
        assert resp.status_code == httpx.codes.CREATED, resp.text

    return project_id


@pytest.mark.asyncio
async def test_loki_logs_land_in_analytics_hourly(api_client, running_application):
    """Real logs in Loki produce a real analytics_hourly row for the project."""
    project_id = running_application

    bucket_end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    bucket_start = bucket_end - timedelta(hours=1)
    moment = bucket_start + timedelta(minutes=10)

    await _push_logs(
        project_id,
        SERVICE_NAME,
        [
            (
                moment,
                {
                    "event": "request",
                    "status_code": 200,
                    "duration_ms": 12.0,
                    "user_id": "u1",
                    "path": "/items",
                },
            ),
            (
                moment + timedelta(seconds=1),
                {
                    "event": "request",
                    "status_code": 200,
                    "duration_ms": 40.0,
                    "user_id": "u2",
                    "path": "/items",
                },
            ),
            (
                moment + timedelta(seconds=2),
                {
                    "event": "request",
                    "status_code": 500,
                    "duration_ms": 90.0,
                    "user_id": "u1",
                    "path": "/boom",
                },
            ),
            (moment + timedelta(seconds=3), {"event": "startup", "message": "not a request"}),
        ],
    )

    loki = LokiClient()
    try:
        await _wait_for_logs(loki, project_id, bucket_start, bucket_end, expected=4)

        cycle = await analytics_aggregator._aggregate_hourly(loki, bucket_start, bucket_end)
    finally:
        await loki.close()

    assert project_id in cycle.attempted
    assert cycle.failed == set()

    rows = await api_client.get_analytics_hourly(
        project_id,
        start=bucket_start.isoformat(),
        end=bucket_end.isoformat(),
    )
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["service_name"] == SERVICE_NAME
    assert row["total_requests"] == 3  # the startup event is not a request
    assert row["error_count"] == 1
    assert row["unique_users"] == 2
    assert row["new_users"] == 2
    assert row["p95_ms"] is not None
    assert {e["path"] for e in row["top_endpoints"]} == {"/items", "/boom"}

    known = await api_client.get_known_users(project_id)
    assert len(known) == 2

    # A cycle that collected this project reports it as healthy to the LK.
    await analytics_aggregator._record_heartbeat(datetime.now(UTC), cycle.failed)
    heartbeat = await _read_heartbeat(api_client)
    assert collection_state(heartbeat, datetime.now(UTC), project_id) is CollectionState.OK


@pytest.mark.asyncio
async def test_unreachable_loki_marks_the_project_as_failing(api_client, running_application):
    """A dead Loki must not leave the LK reporting healthy collection."""
    project_id = running_application

    bucket_end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    bucket_start = bucket_end - timedelta(hours=1)

    dead_loki = LokiClient(base_url="http://loki:3999")
    try:
        cycle = await analytics_aggregator._aggregate_hourly(dead_loki, bucket_start, bucket_end)
    finally:
        await dead_loki.close()

    assert project_id in cycle.failed

    rows = await api_client.get_analytics_hourly(
        project_id,
        start=bucket_start.isoformat(),
        end=bucket_end.isoformat(),
    )
    assert rows == []

    await analytics_aggregator._record_heartbeat(datetime.now(UTC), cycle.failed)
    heartbeat = await _read_heartbeat(api_client)
    assert collection_state(heartbeat, datetime.now(UTC), project_id) is CollectionState.FAILING


async def _read_heartbeat(api_client):
    async with httpx.AsyncClient(
        base_url=api_client.base_url,
        timeout=30.0,
        headers={"X-Internal-Key": INTERNAL_KEY},
    ) as client:
        resp = await client.get(f"/api/system-configs/{ANALYTICS_HEARTBEAT_KEY}")
        assert resp.status_code == httpx.codes.OK, resp.text
        return decode_heartbeat(resp.json()["value"])

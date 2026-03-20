"""Service test — analytics CRUD endpoints against real DB.

Tests the full lifecycle: upsert hourly, upsert daily, batch upsert known users,
query, cleanup.
"""

from http import HTTPStatus

import pytest

pytestmark = pytest.mark.asyncio

TASK_TEST_PROJECT_ID = "00000000-0000-0000-0000-000000000001"
BUCKET = "2026-03-20T14:00:00Z"
SERVICE_NAME = "backend"


@pytest.fixture
def hourly_payload():
    return {
        "project_id": TASK_TEST_PROJECT_ID,
        "service_name": SERVICE_NAME,
        "bucket": BUCKET,
        "total_requests": 100,
        "error_count": 5,
        "unique_users": 10,
        "new_users": 3,
        "p50_ms": 12.5,
        "p95_ms": 45.0,
        "p99_ms": 120.0,
        "top_endpoints": [{"path": "/start", "count": 42}],
    }


class TestAnalyticsHourly:
    async def test_upsert_creates_row(self, async_client, _tasks_project, hourly_payload):
        resp = await async_client.post("/api/analytics/hourly", json=hourly_payload)
        assert resp.status_code == HTTPStatus.CREATED
        data = resp.json()
        assert data["total_requests"] == 100
        assert data["service_name"] == SERVICE_NAME
        assert data["project_id"] == TASK_TEST_PROJECT_ID

    async def test_upsert_updates_on_conflict(self, async_client, _tasks_project, hourly_payload):
        # First insert
        await async_client.post("/api/analytics/hourly", json=hourly_payload)

        # Update with new values
        hourly_payload["total_requests"] = 200
        hourly_payload["error_count"] = 10
        resp = await async_client.post("/api/analytics/hourly", json=hourly_payload)
        assert resp.status_code == HTTPStatus.CREATED
        data = resp.json()
        assert data["total_requests"] == 200
        assert data["error_count"] == 10

    async def test_list_by_project(self, async_client, _tasks_project, hourly_payload):
        await async_client.post("/api/analytics/hourly", json=hourly_payload)

        resp = await async_client.get(
            "/api/analytics/hourly",
            params={"project_id": TASK_TEST_PROJECT_ID},
        )
        assert resp.status_code == HTTPStatus.OK
        rows = resp.json()
        assert len(rows) >= 1
        assert all(r["project_id"] == TASK_TEST_PROJECT_ID for r in rows)

    async def test_list_with_time_range(self, async_client, _tasks_project, hourly_payload):
        await async_client.post("/api/analytics/hourly", json=hourly_payload)

        resp = await async_client.get(
            "/api/analytics/hourly",
            params={
                "project_id": TASK_TEST_PROJECT_ID,
                "start": "2026-03-20T00:00:00Z",
                "end": "2026-03-21T00:00:00Z",
            },
        )
        assert resp.status_code == HTTPStatus.OK
        assert len(resp.json()) >= 1

    async def test_delete_old(self, async_client, _tasks_project, hourly_payload):
        await async_client.post("/api/analytics/hourly", json=hourly_payload)

        # Delete older than 0 days (everything)
        resp = await async_client.delete(
            "/api/analytics/hourly",
            params={"older_than_days": 0},
        )
        assert resp.status_code == HTTPStatus.OK
        assert resp.json()["deleted"] >= 1


class TestAnalyticsDaily:
    async def test_upsert_creates_row(self, async_client, _tasks_project):
        payload = {
            "project_id": TASK_TEST_PROJECT_ID,
            "date": "2026-03-20",
            "total_requests": 1000,
            "error_count": 50,
            "unique_users": 42,
            "new_users": 10,
            "dau": 42,
            "returning_users": 32,
            "p95_ms": 80.0,
            "error_rate": 0.05,
        }
        resp = await async_client.post("/api/analytics/daily", json=payload)
        assert resp.status_code == HTTPStatus.CREATED
        data = resp.json()
        assert data["total_requests"] == 1000
        assert data["dau"] == 42

    async def test_upsert_updates_on_conflict(self, async_client, _tasks_project):
        payload = {
            "project_id": TASK_TEST_PROJECT_ID,
            "date": "2026-03-19",
            "total_requests": 500,
            "error_count": 20,
            "unique_users": 30,
            "new_users": 5,
            "dau": 30,
            "returning_users": 25,
            "p95_ms": 60.0,
            "error_rate": 0.04,
        }
        await async_client.post("/api/analytics/daily", json=payload)

        payload["total_requests"] = 600
        resp = await async_client.post("/api/analytics/daily", json=payload)
        assert resp.status_code == HTTPStatus.CREATED
        assert resp.json()["total_requests"] == 600

    async def test_list_by_project(self, async_client, _tasks_project):
        payload = {
            "project_id": TASK_TEST_PROJECT_ID,
            "date": "2026-03-18",
            "total_requests": 200,
            "error_count": 5,
            "unique_users": 15,
            "new_users": 3,
            "dau": 15,
            "returning_users": 12,
            "p95_ms": 40.0,
            "error_rate": 0.025,
        }
        await async_client.post("/api/analytics/daily", json=payload)

        resp = await async_client.get(
            "/api/analytics/daily",
            params={"project_id": TASK_TEST_PROJECT_ID},
        )
        assert resp.status_code == HTTPStatus.OK
        assert len(resp.json()) >= 1

    async def test_delete_old(self, async_client, _tasks_project):
        resp = await async_client.delete(
            "/api/analytics/daily",
            params={"older_than_days": 0},
        )
        assert resp.status_code == HTTPStatus.OK


class TestAnalyticsKnownUsers:
    async def test_batch_upsert(self, async_client, _tasks_project):
        payload = {
            "project_id": TASK_TEST_PROJECT_ID,
            "users": [
                {
                    "user_id_hash": "abc123def456",
                    "first_seen": "2026-03-20T10:00:00Z",
                    "last_seen": "2026-03-20T14:00:00Z",
                },
                {
                    "user_id_hash": "xyz789abc012",
                    "first_seen": "2026-03-20T12:00:00Z",
                    "last_seen": "2026-03-20T14:00:00Z",
                },
            ],
        }
        resp = await async_client.post("/api/analytics/known-users", json=payload)
        assert resp.status_code == HTTPStatus.CREATED
        assert resp.json()["upserted"] == 2

    async def test_upsert_updates_last_seen(self, async_client, _tasks_project):
        payload = {
            "project_id": TASK_TEST_PROJECT_ID,
            "users": [
                {
                    "user_id_hash": "abc123def456",
                    "first_seen": "2026-03-20T10:00:00Z",
                    "last_seen": "2026-03-20T16:00:00Z",  # later
                },
            ],
        }
        resp = await async_client.post("/api/analytics/known-users", json=payload)
        assert resp.status_code == HTTPStatus.CREATED

        # Verify last_seen was updated
        resp = await async_client.get(
            "/api/analytics/known-users",
            params={"project_id": TASK_TEST_PROJECT_ID},
        )
        users = resp.json()
        matching = [u for u in users if u["user_id_hash"] == "abc123def456"]
        assert len(matching) == 1
        assert "16:00:00" in matching[0]["last_seen"]

    async def test_list_by_project(self, async_client, _tasks_project):
        # Seed data
        payload = {
            "project_id": TASK_TEST_PROJECT_ID,
            "users": [
                {
                    "user_id_hash": "list_test_hash",
                    "first_seen": "2026-03-20T10:00:00Z",
                    "last_seen": "2026-03-20T14:00:00Z",
                },
            ],
        }
        await async_client.post("/api/analytics/known-users", json=payload)

        resp = await async_client.get(
            "/api/analytics/known-users",
            params={"project_id": TASK_TEST_PROJECT_ID},
        )
        assert resp.status_code == HTTPStatus.OK
        assert len(resp.json()) >= 1

    async def test_empty_batch(self, async_client, _tasks_project):
        payload = {
            "project_id": TASK_TEST_PROJECT_ID,
            "users": [],
        }
        resp = await async_client.post("/api/analytics/known-users", json=payload)
        assert resp.status_code == HTTPStatus.CREATED
        assert resp.json()["upserted"] == 0

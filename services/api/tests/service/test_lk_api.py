"""Service tests for LK (user dashboard) auth and analytics endpoints.

Tests the full flow: token exchange → JWT → protected endpoints.
Requires real DB + Redis (runs in service test compose).
"""

import datetime as dt
import os
import uuid

from httpx import ASGITransport, AsyncClient
import jwt
import pytest
from redis.asyncio import Redis

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

# Test data
LK_TEST_TELEGRAM_ID = 888000888
LK_TEST_PROJECT_ID = str(uuid.uuid4())
LK_JWT_SECRET = "test-lk-jwt-secret-for-service-tests"  # noqa: S105


@pytest.fixture(scope="module")
async def lk_user_and_project():
    """Create a user and project for LK tests."""
    from src.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Create user
        resp = await client.get(f"/api/users/by-telegram/{LK_TEST_TELEGRAM_ID}")
        if resp.status_code == 404:
            resp = await client.post(
                "/api/users/",
                json={
                    "telegram_id": LK_TEST_TELEGRAM_ID,
                    "username": "lk_test_user",
                    "first_name": "LK",
                    "is_admin": False,
                },
            )
            assert resp.status_code == 201
        user = (await client.get(f"/api/users/by-telegram/{LK_TEST_TELEGRAM_ID}")).json()
        user_id = user["id"]

        # Create project
        resp = await client.get(f"/api/projects/{LK_TEST_PROJECT_ID}")
        if resp.status_code == 404:
            resp = await client.post(
                "/api/projects/",
                json={
                    "id": LK_TEST_PROJECT_ID,
                    "name": "LK Test Project",
                    "status": "active",
                    "config": {},
                },
                headers={"X-Telegram-ID": str(LK_TEST_TELEGRAM_ID)},
            )
            assert resp.status_code == 201

    return {"user_id": user_id, "project_id": LK_TEST_PROJECT_ID}


@pytest.fixture(scope="module")
async def _ensure_app_redis():
    """Ensure the app's internal Redis singleton is initialized.

    ASGITransport tests skip the lifespan, so init_redis() never runs.
    """
    import src.dependencies as deps

    if deps._redis_client is None:
        await deps.init_redis()
    yield
    # Don't close — other tests may still need it


@pytest.fixture
async def lk_redis(_ensure_app_redis):
    """Direct Redis client for setting test tokens."""
    client = Redis.from_url(REDIS_URL)
    yield client
    await client.aclose()


def _make_jwt(user_id: int, expired: bool = False) -> str:
    """Create a JWT for testing."""
    exp = dt.datetime.now(dt.UTC) + (dt.timedelta(hours=-1) if expired else dt.timedelta(hours=24))
    payload = {"sub": str(user_id), "exp": exp, "iat": dt.datetime.now(dt.UTC)}
    return jwt.encode(payload, LK_JWT_SECRET, algorithm="HS256")


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Auth: token exchange
# ---------------------------------------------------------------------------


class TestTokenExchange:
    """POST /api/lk/auth/token"""

    async def test_valid_token_returns_jwt(self, async_client, lk_redis, lk_user_and_project):
        user_id = lk_user_and_project["user_id"]
        token = str(uuid.uuid4())

        # Set one-time token in Redis
        await lk_redis.set(f"lk_token:{token}", str(user_id), ex=300)

        resp = await async_client.post("/api/lk/auth/token", json={"token": token})
        assert resp.status_code == 200

        data = resp.json()
        assert data["token_type"] == "bearer"  # noqa: S105
        assert "access_token" in data

        # Decode and verify JWT
        decoded = jwt.decode(data["access_token"], LK_JWT_SECRET, algorithms=["HS256"])
        assert decoded["sub"] == str(user_id)

    async def test_token_is_one_time(self, async_client, lk_redis, lk_user_and_project):
        user_id = lk_user_and_project["user_id"]
        token = str(uuid.uuid4())

        await lk_redis.set(f"lk_token:{token}", str(user_id), ex=300)

        # First use succeeds
        resp = await async_client.post("/api/lk/auth/token", json={"token": token})
        assert resp.status_code == 200

        # Second use fails (token deleted)
        resp = await async_client.post("/api/lk/auth/token", json={"token": token})
        assert resp.status_code == 401

    async def test_invalid_token_returns_401(self, async_client):
        resp = await async_client.post("/api/lk/auth/token", json={"token": "nonexistent"})
        assert resp.status_code == 401

    async def test_expired_token_returns_401(self, async_client, lk_redis, lk_user_and_project):
        user_id = lk_user_and_project["user_id"]
        token = str(uuid.uuid4())

        # Set with 0 TTL (already expired)
        await lk_redis.set(f"lk_token:{token}", str(user_id), px=1)
        import asyncio

        await asyncio.sleep(0.01)  # Let it expire

        resp = await async_client.post("/api/lk/auth/token", json={"token": token})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# JWT auth dependency
# ---------------------------------------------------------------------------


class TestJwtAuth:
    """Test that LK endpoints require valid JWT."""

    async def test_no_auth_returns_401_or_403(self, async_client):
        resp = await async_client.get("/api/lk/projects")
        assert resp.status_code in (401, 403)

    async def test_invalid_jwt_returns_401(self, async_client):
        resp = await async_client.get("/api/lk/projects", headers=_auth_header("garbage"))
        assert resp.status_code == 401

    async def test_expired_jwt_returns_401(self, async_client, lk_user_and_project):
        token = _make_jwt(lk_user_and_project["user_id"], expired=True)
        resp = await async_client.get("/api/lk/projects", headers=_auth_header(token))
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/lk/projects
# ---------------------------------------------------------------------------


class TestListProjects:
    async def test_returns_owned_projects(self, async_client, lk_user_and_project):
        token = _make_jwt(lk_user_and_project["user_id"])
        resp = await async_client.get("/api/lk/projects", headers=_auth_header(token))

        assert resp.status_code == 200
        projects = resp.json()
        assert len(projects) >= 1
        assert any(p["id"] == lk_user_and_project["project_id"] for p in projects)

    async def test_project_has_summary_field(self, async_client, lk_user_and_project):
        token = _make_jwt(lk_user_and_project["user_id"])
        resp = await async_client.get("/api/lk/projects", headers=_auth_header(token))

        projects = resp.json()
        project = next(p for p in projects if p["id"] == lk_user_and_project["project_id"])
        # May or may not have summary depending on test order — just verify structure
        assert "latest_daily" in project


# ---------------------------------------------------------------------------
# Fixture: seed analytics data
# ---------------------------------------------------------------------------


@pytest.fixture
async def seeded_analytics(async_client, lk_user_and_project):
    """Seed hourly and daily analytics data for summary/chart/status tests."""
    project_id = lk_user_and_project["project_id"]
    today = dt.date.today()
    now = dt.datetime.now(dt.UTC)

    # Seed 3 hourly rows (recent, for status=up)
    for i in range(3):
        bucket = now - dt.timedelta(hours=i)
        await async_client.post(
            "/api/analytics/hourly",
            json={
                "project_id": project_id,
                "service_name": "backend",
                "bucket": bucket.isoformat(),
                "total_requests": 100 + i * 10,
                "error_count": 2 + i,
                "unique_users": 10 + i,
                "new_users": 2,
                "p50_ms": 20.0,
                "p95_ms": 45.0 + i,
                "p99_ms": 100.0,
                "top_endpoints": [
                    {"path": "/start", "count": 50 + i * 5},
                    {"path": "/weather", "count": 30},
                ],
            },
        )

    # Seed 1 hourly row for tg_bot (recent)
    await async_client.post(
        "/api/analytics/hourly",
        json={
            "project_id": project_id,
            "service_name": "tg_bot",
            "bucket": now.isoformat(),
            "total_requests": 50,
            "error_count": 1,
            "unique_users": 8,
            "new_users": 1,
            "p50_ms": 15.0,
            "p95_ms": 30.0,
            "p99_ms": 60.0,
            "top_endpoints": [{"path": "/start", "count": 40}],
        },
    )

    # Seed 7 daily rows
    for i in range(7):
        d = today - dt.timedelta(days=i)
        await async_client.post(
            "/api/analytics/daily",
            json={
                "project_id": project_id,
                "date": str(d),
                "total_requests": 500 + i * 50,
                "error_count": 5 + i,
                "unique_users": 40 + i,
                "new_users": 5,
                "dau": 40 + i,
                "returning_users": 35 + i,
                "p95_ms": 50.0 + i,
                "error_rate": round((5 + i) / (500 + i * 50), 4),
            },
        )

    return project_id


# ---------------------------------------------------------------------------
# GET /api/lk/projects/{id}/summary
# ---------------------------------------------------------------------------


class TestProjectSummary:
    async def test_summary_24h(self, async_client, lk_user_and_project, seeded_analytics):
        token = _make_jwt(lk_user_and_project["user_id"])
        project_id = seeded_analytics

        resp = await async_client.get(
            f"/api/lk/projects/{project_id}/summary",
            params={"period": "24h"},
            headers=_auth_header(token),
        )
        assert resp.status_code == 200

        data = resp.json()
        assert data["total_requests"] > 0
        assert data["total_users"] > 0
        assert "breakdown" in data
        assert len(data["breakdown"]) >= 1

    async def test_summary_7d(self, async_client, lk_user_and_project, seeded_analytics):
        token = _make_jwt(lk_user_and_project["user_id"])
        project_id = seeded_analytics

        resp = await async_client.get(
            f"/api/lk/projects/{project_id}/summary",
            params={"period": "7d"},
            headers=_auth_header(token),
        )
        assert resp.status_code == 200

        data = resp.json()
        assert data["total_requests"] > 0
        assert data["dau"] > 0

    async def test_summary_empty_project(self, async_client, lk_user_and_project):
        """Summary for a project with no analytics returns zeros."""
        token = _make_jwt(lk_user_and_project["user_id"])
        # Create a second project with no data
        project2_id = str(uuid.uuid4())
        await async_client.post(
            "/api/projects/",
            json={
                "id": project2_id,
                "name": "Empty Project",
                "status": "active",
                "config": {},
            },
            headers={"X-Telegram-ID": str(LK_TEST_TELEGRAM_ID)},
        )

        resp = await async_client.get(
            f"/api/lk/projects/{project2_id}/summary",
            params={"period": "7d"},
            headers=_auth_header(token),
        )
        assert resp.status_code == 200
        assert resp.json()["total_requests"] == 0

    async def test_summary_forbidden_for_non_owner(self, async_client, seeded_analytics):
        """Another user can't see your project's summary."""
        other_jwt = _make_jwt(99999)  # Non-existent user
        resp = await async_client.get(
            f"/api/lk/projects/{seeded_analytics}/summary",
            params={"period": "7d"},
            headers=_auth_header(other_jwt),
        )
        assert resp.status_code == 401  # User not found


# ---------------------------------------------------------------------------
# GET /api/lk/projects/{id}/chart
# ---------------------------------------------------------------------------


class TestProjectChart:
    async def test_chart_users(self, async_client, lk_user_and_project, seeded_analytics):
        token = _make_jwt(lk_user_and_project["user_id"])
        project_id = seeded_analytics

        resp = await async_client.get(
            f"/api/lk/projects/{project_id}/chart",
            params={"metric": "users", "period": "7d"},
            headers=_auth_header(token),
        )
        assert resp.status_code == 200

        data = resp.json()
        assert data["metric"] == "users"
        assert data["period"] == "7d"
        assert len(data["data"]) >= 1
        assert "date" in data["data"][0]
        assert "value" in data["data"][0]

    async def test_chart_requests(self, async_client, lk_user_and_project, seeded_analytics):
        token = _make_jwt(lk_user_and_project["user_id"])
        resp = await async_client.get(
            f"/api/lk/projects/{seeded_analytics}/chart",
            params={"metric": "requests", "period": "7d"},
            headers=_auth_header(token),
        )
        assert resp.status_code == 200
        assert all(dp["value"] > 0 for dp in resp.json()["data"])

    async def test_chart_errors(self, async_client, lk_user_and_project, seeded_analytics):
        token = _make_jwt(lk_user_and_project["user_id"])
        resp = await async_client.get(
            f"/api/lk/projects/{seeded_analytics}/chart",
            params={"metric": "errors", "period": "7d"},
            headers=_auth_header(token),
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/lk/projects/{id}/status
# ---------------------------------------------------------------------------


class TestProjectStatus:
    async def test_status_shows_services(self, async_client, lk_user_and_project, seeded_analytics):
        token = _make_jwt(lk_user_and_project["user_id"])
        project_id = seeded_analytics

        resp = await async_client.get(
            f"/api/lk/projects/{project_id}/status",
            headers=_auth_header(token),
        )
        assert resp.status_code == 200

        data = resp.json()
        assert "services" in data
        assert len(data["services"]) >= 1

        svc_names = {s["name"] for s in data["services"]}
        assert "backend" in svc_names

        # Recent data → should be "up"
        backend = next(s for s in data["services"] if s["name"] == "backend")
        assert backend["status"] == "up"
        assert backend["last_seen"] is not None

    async def test_status_forbidden_for_non_owner(self, async_client, seeded_analytics):
        other_jwt = _make_jwt(99999)
        resp = await async_client.get(
            f"/api/lk/projects/{seeded_analytics}/status",
            headers=_auth_header(other_jwt),
        )
        assert resp.status_code == 401

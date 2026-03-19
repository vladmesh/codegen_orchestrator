"""Integration tests for Application health history endpoints."""

from http import HTTPStatus

import pytest


@pytest.fixture
async def test_app(async_client, _tasks_project):
    """Create a test server + repository + application, yield app ID."""
    project_id = _tasks_project

    # Ensure server
    handle = "test-app-health-srv"
    resp = await async_client.post(
        "/api/servers/",
        json={
            "handle": handle,
            "host": "test-app.example.com",
            "public_ip": "10.0.0.100",
            "status": "active",
            "is_managed": True,
        },
    )
    if resp.status_code not in (HTTPStatus.CREATED, HTTPStatus.BAD_REQUEST):
        pytest.fail(f"Unexpected status creating server: {resp.status_code}")

    # Ensure repository (needs project to exist)
    repo_id = "repo-app-health-test"
    resp = await async_client.post(
        "/api/repositories/",
        json={
            "project_id": project_id,
            "name": "test-app-health-repo",
            "git_url": "https://github.com/test/test-app.git",
            "role": "primary",
        },
    )
    if resp.status_code == HTTPStatus.CREATED:
        repo_id = resp.json()["id"]
    else:
        # Already exists — find by name
        resp = await async_client.get(f"/api/repositories/?project_id={project_id}")
        if resp.status_code == HTTPStatus.OK:
            repos = resp.json()
            for r in repos:
                if r["name"] == "test-app-health-repo":
                    repo_id = r["id"]
                    break

    # Create application
    resp = await async_client.post(
        "/api/applications/",
        json={
            "repo_id": repo_id,
            "server_handle": handle,
            "service_name": "health-test-app",
            "status": "running",
        },
    )
    if resp.status_code == HTTPStatus.CREATED:
        app_id = resp.json()["id"]
    else:
        # Already exists — find it
        resp = await async_client.get(
            f"/api/applications/?server_handle={handle}&repo_id={repo_id}"
        )
        assert resp.status_code == HTTPStatus.OK
        apps = resp.json()
        app_id = apps[0]["id"]

    yield app_id


@pytest.mark.asyncio
async def test_health_history_roundtrip(async_client, test_app):
    """POST health snapshot, GET history, verify content."""
    app_id = test_app
    metrics = {
        "response_time_ms": 142,
        "status_code": 200,
        "healthy": True,
        "ssl_days_remaining": 45,
    }

    # POST snapshot
    resp = await async_client.post(
        f"/api/applications/{app_id}/health-history",
        json={"metrics": metrics},
    )
    assert resp.status_code == HTTPStatus.CREATED
    created = resp.json()
    assert created["application_id"] == app_id
    assert created["metrics"]["response_time_ms"] == 142
    assert created["id"] is not None

    # GET history
    resp = await async_client.get(f"/api/applications/{app_id}/health-history?hours=1")
    assert resp.status_code == HTTPStatus.OK
    history = resp.json()
    assert len(history) >= 1
    assert history[0]["metrics"]["response_time_ms"] == 142


@pytest.mark.asyncio
async def test_health_history_not_found(async_client):
    """GET history for non-existent application returns 404."""
    resp = await async_client.get("/api/applications/999999/health-history")
    assert resp.status_code == HTTPStatus.NOT_FOUND


@pytest.mark.asyncio
async def test_patch_application_health_fields(async_client, test_app):
    """PATCH application with health fields, verify round-trip."""
    app_id = test_app
    resp = await async_client.patch(
        f"/api/applications/{app_id}",
        json={
            "response_time_ms": 95,
            "ssl_expires_at": "2026-06-15T00:00:00Z",
            "uptime_pct_24h": 99.8,
        },
    )
    assert resp.status_code == HTTPStatus.OK
    data = resp.json()
    assert data["response_time_ms"] == 95
    assert data["uptime_pct_24h"] == 99.8
    assert data["ssl_expires_at"] is not None

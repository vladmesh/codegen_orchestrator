"""Integration test: multi-user project isolation.

Verifies that:
1. Projects are owned by the creating user
2. Users can only see their own projects
3. Users cannot access other users' projects (403)
4. System calls (no header) still return all projects
"""

import contextlib

import pytest


@pytest.fixture
async def seed_users(api_client):
    """Create two test users via upsert."""
    users = []
    for tg_id in (111_000, 222_000):
        resp = await api_client.post(
            "/api/users/upsert",
            json={"telegram_id": tg_id, "username": f"testuser-{tg_id}"},
            headers={"X-Telegram-ID": str(tg_id)},
        )
        assert resp.status_code in (200, 201), f"Failed to upsert user {tg_id}: {resp.text}"
        users.append(resp.json())
    return users


@pytest.fixture
async def user_projects(api_client, seed_users):
    """Create one project per user. Cleanup after test."""
    created = []

    for tg_id, proj_id, name in [
        (111_000, "iso-proj-a", "proj-a"),
        (222_000, "iso-proj-b", "proj-b"),
    ]:
        resp = await api_client.post(
            "/api/projects/",
            json={
                "id": proj_id,
                "name": name,
                "status": "draft",
                "config": {"modules": ["backend"]},
            },
            headers={"X-Telegram-ID": str(tg_id)},
        )
        assert resp.status_code == 201, f"Failed to create {proj_id}: {resp.text}"
        created.append(resp.json())

    yield created

    for proj in created:
        with contextlib.suppress(Exception):
            await api_client.delete(f"/api/projects/{proj['id']}")


@pytest.mark.asyncio
async def test_user_sees_only_own_projects(api_client, user_projects):
    """User 111_000 lists projects → sees only proj-a."""
    resp = await api_client.get("/api/projects/", headers={"X-Telegram-ID": "111000"})
    assert resp.status_code == 200  # noqa: PLR2004
    projects = resp.json()
    names = {p["name"] for p in projects}
    assert "proj-a" in names
    assert "proj-b" not in names


@pytest.mark.asyncio
async def test_other_user_sees_only_own_projects(api_client, user_projects):
    """User 222_000 lists projects → sees only proj-b."""
    resp = await api_client.get("/api/projects/", headers={"X-Telegram-ID": "222000"})
    assert resp.status_code == 200  # noqa: PLR2004
    projects = resp.json()
    names = {p["name"] for p in projects}
    assert "proj-b" in names
    assert "proj-a" not in names


@pytest.mark.asyncio
async def test_cross_user_access_denied(api_client, user_projects):
    """User 222_000 tries to GET proj-a → 403."""
    resp = await api_client.get("/api/projects/iso-proj-a", headers={"X-Telegram-ID": "222000"})
    assert resp.status_code == 403  # noqa: PLR2004


@pytest.mark.asyncio
async def test_system_call_returns_all(api_client, user_projects):
    """No X-Telegram-ID header → returns all projects (system/internal call)."""
    resp = await api_client.get("/api/projects/")
    assert resp.status_code == 200  # noqa: PLR2004
    projects = resp.json()
    names = {p["name"] for p in projects}
    assert "proj-a" in names
    assert "proj-b" in names


@pytest.mark.asyncio
async def test_create_without_header_returns_400(api_client):
    """POST /api/projects/ without X-Telegram-ID → 400."""
    resp = await api_client.post(
        "/api/projects/",
        json={
            "id": "iso-no-owner",
            "name": "no-owner",
            "status": "draft",
            "config": {},
        },
    )
    assert resp.status_code == 400  # noqa: PLR2004

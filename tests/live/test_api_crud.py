"""Step 2: API CRUD — baseline, should pass immediately."""

import secrets
import uuid

import pytest


@pytest.mark.asyncio
async def test_create_and_get_project(api):
    """Create a project via API and retrieve it."""
    project_id = str(uuid.uuid4())
    name = f"live-crud-{secrets.token_hex(4)}"

    resp = await api.post(
        "/api/projects/",
        json={"id": project_id, "name": name, "status": "draft", "config": {}},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] == project_id
    assert data["name"] == name

    # GET
    resp = await api.get(f"/api/projects/{project_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == name

    # cleanup
    await api.delete(f"/api/projects/{project_id}")


@pytest.mark.asyncio
async def test_create_story_for_project(api, test_project):
    """Create a story linked to a project."""
    resp = await api.post(
        "/api/stories/",
        json={
            "project_id": test_project["id"],
            "title": "Live test story",
            "description": "Automated live test",
        },
    )
    assert resp.status_code == 201
    story = resp.json()
    assert story["status"] == "created"
    assert story["project_id"] == test_project["id"]

    # verify list
    resp = await api.get(f"/api/stories/?project_id={test_project['id']}")
    assert resp.status_code == 200
    stories = resp.json()
    assert any(s["id"] == story["id"] for s in stories)


@pytest.mark.asyncio
async def test_create_and_list_tasks(api, test_project):
    """Create tasks and list them by project."""
    task_resp = await api.post(
        "/api/tasks/",
        json={
            "project_id": test_project["id"],
            "title": "Live test task",
            "description": "Test task for live tests",
        },
    )
    assert task_resp.status_code == 201
    task = task_resp.json()
    assert task["status"] == "backlog"

    # list by project
    resp = await api.get(f"/api/tasks/?project_id={test_project['id']}")
    assert resp.status_code == 200
    tasks = resp.json()
    assert any(t["id"] == task["id"] for t in tasks)


@pytest.mark.asyncio
async def test_task_transitions(api, test_project):
    """Task follows valid state machine: backlog → todo → in_dev → done."""
    resp = await api.post(
        "/api/tasks/",
        json={
            "project_id": test_project["id"],
            "title": "Transition test task",
        },
    )
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    # backlog → todo
    resp = await api.post(f"/api/tasks/{task_id}/transition?to_status=todo")
    assert resp.status_code == 200
    assert resp.json()["status"] == "todo"

    # todo → in_dev (via start)
    resp = await api.post(f"/api/tasks/{task_id}/start")
    assert resp.status_code == 200
    assert resp.json()["status"] == "in_dev"

    # in_dev → failed (via fail)
    resp = await api.post(f"/api/tasks/{task_id}/fail")
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"


@pytest.mark.asyncio
async def test_upsert_user(api):
    """Upsert user creates or updates."""
    tg_id = 900_000_000 + secrets.randbelow(100_000)
    resp = await api.post(
        "/api/users/upsert",
        json={
            "telegram_id": tg_id,
            "username": "live_test_user",
            "first_name": "Live",
            "last_name": "Test",
        },
    )
    assert resp.status_code == 200
    user = resp.json()
    assert user["telegram_id"] == tg_id

    # upsert again — should not fail
    resp = await api.post(
        "/api/users/upsert",
        json={
            "telegram_id": tg_id,
            "username": "live_test_user_updated",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["username"] == "live_test_user_updated"

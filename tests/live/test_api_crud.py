"""Step 2: API CRUD — baseline, should pass immediately."""

import secrets
import uuid

import pytest

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus


@pytest.mark.asyncio
async def test_create_and_get_project(api):
    """Create a project via API and retrieve it."""
    project_id = str(uuid.uuid4())
    name = f"live-crud-{secrets.token_hex(4)}"

    resp = await api.post(
        "/api/projects/",
        json={"id": project_id, "title": name, "status": ProjectStatus.DRAFT, "config": {}},
    )
    resp.raise_for_status()
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] == project_id
    assert data["title"] == name

    # GET
    resp = await api.get(f"/api/projects/{project_id}")
    resp.raise_for_status()
    assert resp.status_code == 200
    assert resp.json()["title"] == name

    # cleanup
    resp = await api.delete(f"/api/projects/{project_id}")
    resp.raise_for_status()


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
    resp.raise_for_status()
    assert resp.status_code == 201
    story = resp.json()
    assert story["status"] == StoryStatus.CREATED
    assert story["project_id"] == test_project["id"]

    # verify list
    resp = await api.get(f"/api/stories/?project_id={test_project['id']}")
    resp.raise_for_status()
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
    task_resp.raise_for_status()
    assert task_resp.status_code == 201
    task = task_resp.json()
    assert task["status"] == TaskStatus.BACKLOG

    # list by project
    resp = await api.get(f"/api/tasks/?project_id={test_project['id']}")
    resp.raise_for_status()
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
    resp.raise_for_status()
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    # backlog → todo
    resp = await api.post(f"/api/tasks/{task_id}/transition?to_status={TaskStatus.TODO}")
    resp.raise_for_status()
    assert resp.status_code == 200
    assert resp.json()["status"] == TaskStatus.TODO

    # todo → in_dev (via start)
    resp = await api.post(f"/api/tasks/{task_id}/start")
    resp.raise_for_status()
    assert resp.status_code == 200
    assert resp.json()["status"] == TaskStatus.IN_DEV

    # in_dev → failed (via fail)
    resp = await api.post(f"/api/tasks/{task_id}/fail")
    resp.raise_for_status()
    assert resp.status_code == 200
    assert resp.json()["status"] == TaskStatus.FAILED


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
    resp.raise_for_status()
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
    resp.raise_for_status()
    assert resp.status_code == 200
    assert resp.json()["username"] == "live_test_user_updated"

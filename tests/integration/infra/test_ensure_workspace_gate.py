"""Integration tests for the ensure-workspace gate.

Flow being tested:
1. notify-workspace-deleted API endpoint clears workspace_ready from project config
2. scaffold_trigger publishes mode=ensure for ACTIVE projects with TODO tasks
3. task_dispatcher skips tasks when workspace_ready is falsy
"""

import asyncio
import json
import uuid

import pytest

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.task import TaskStatus
from shared.queues import SCAFFOLD_QUEUE

TEST_TELEGRAM_ID = "999888777"
_HEADERS = {"X-Telegram-ID": TEST_TELEGRAM_ID}


@pytest.fixture(autouse=True)
async def _ensure_test_user(api_client):
    """Create test user if it doesn't exist (required for project creation)."""
    resp = await api_client.get(f"/api/users/by-telegram/{TEST_TELEGRAM_ID}")
    if resp.status_code == 404:
        await api_client.post(
            "/api/users/",
            json={
                "telegram_id": int(TEST_TELEGRAM_ID),
                "username": "int_test_scaffold",
                "first_name": "Test",
                "is_admin": True,
            },
        )


async def _create_project(api_client, *, status="active", config=None):
    """Create a project via API and return its data."""
    resp = await api_client.post(
        "/api/projects/",
        json={
            "name": f"int-test-{uuid.uuid4().hex[:8]}",
            "status": status,
            "config": config or {},
        },
        headers=_HEADERS,
    )
    assert resp.status_code == 201, f"Failed to create project: {resp.text}"
    return resp.json()


async def _create_repository(api_client, project_id):
    """Create a repository linked to a project."""
    resp = await api_client.post(
        "/api/repositories/",
        json={
            "project_id": project_id,
            "name": f"repo-{uuid.uuid4().hex[:8]}",
            "git_url": "https://github.com/org/test-repo",
        },
    )
    assert resp.status_code == 201, f"Failed to create repository: {resp.text}"
    return resp.json()


async def _create_task(api_client, project_id, *, status="todo"):
    """Create a task linked to a project."""
    resp = await api_client.post(
        "/api/tasks/",
        json={
            "title": f"int-task-{uuid.uuid4().hex[:8]}",
            "project_id": project_id,
            "status": status,
            "priority": 1,
        },
    )
    assert resp.status_code == 201, f"Failed to create task: {resp.text}"
    return resp.json()


@pytest.mark.asyncio
async def test_notify_workspace_deleted_clears_flag(api_client):
    """POST /repositories/{repo_id}/notify-workspace-deleted clears workspace_ready in DB.

    GIVEN: Project with workspace_ready=true and a linked repository
    WHEN:  notify-workspace-deleted is called with the repo_id
    THEN:  Project config no longer has workspace_ready
    """
    project = await _create_project(
        api_client,
        config={"workspace_ready": True, "modules": ["backend"]},
    )
    repo = await _create_repository(api_client, project["id"])

    resp = await api_client.post(f"/api/repositories/{repo['id']}/notify-workspace-deleted")
    assert resp.status_code == 200
    assert resp.json()["workspace_ready"] is False

    # Verify via GET that config was actually updated in DB
    resp = await api_client.get(f"/api/projects/{project['id']}", headers=_HEADERS)
    assert resp.status_code == 200
    config = resp.json()["config"]
    assert "workspace_ready" not in config
    # Other config fields preserved
    assert config["modules"] == ["backend"]


@pytest.mark.asyncio
async def test_scaffold_trigger_publishes_ensure_for_active_project(api_client, redis_client):
    """scaffold_trigger publishes mode=ensure to scaffold:queue for ACTIVE project.

    GIVEN: ACTIVE project with TODO task, linked repo, and workspace_ready not set
    WHEN:  scaffold_trigger runs (via scheduler loop)
    THEN:  ScaffoldMessage with mode=ensure appears on scaffold:queue
    """
    project = await _create_project(api_client, status=ProjectStatus.ACTIVE)
    await _create_repository(api_client, project["id"])
    await _create_task(api_client, project["id"], status=TaskStatus.TODO)

    # Wait for scheduler's scaffold_trigger cycle to fire (runs every 30s in dispatcher loop)
    max_attempts = 40
    for _attempt in range(max_attempts):
        messages = await redis_client.xrange(SCAFFOLD_QUEUE, count=100)
        for _msg_id, data in messages:
            payload = json.loads(data.get("data", "{}"))
            if payload.get("project_id") == project["id"] and payload.get("mode") == "ensure":
                return

        await asyncio.sleep(1)

    pytest.fail(
        f"No scaffold:queue message with mode=ensure for project {project['id']} "
        f"within {max_attempts} seconds"
    )


@pytest.mark.asyncio
async def test_task_dispatcher_skips_when_workspace_not_ready(api_client, redis_client):
    """task_dispatcher does not dispatch tasks when workspace_ready is falsy.

    GIVEN: ACTIVE project with TODO task but workspace_ready not set
    WHEN:  task_dispatcher runs (via scheduler loop)
    THEN:  Task remains in TODO status (not dispatched to engineering:queue)
    """
    project = await _create_project(api_client, status=ProjectStatus.ACTIVE)
    await _create_repository(api_client, project["id"])
    task = await _create_task(api_client, project["id"], status=TaskStatus.TODO)

    # Wait enough cycles for task_dispatcher to have run
    await asyncio.sleep(35)

    # Task should still be TODO (not picked up because workspace_ready is not set)
    resp = await api_client.get(f"/api/tasks/{task['id']}")
    assert resp.status_code == 200
    assert resp.json()["status"] == TaskStatus.TODO


@pytest.mark.asyncio
async def test_task_dispatcher_dispatches_when_workspace_ready(api_client, redis_client):
    """task_dispatcher dispatches tasks when workspace_ready is true.

    GIVEN: ACTIVE project with TODO task and workspace_ready=true
    WHEN:  task_dispatcher runs
    THEN:  Task transitions out of TODO (dispatched to engineering:queue)
    """
    project = await _create_project(
        api_client,
        status=ProjectStatus.ACTIVE,
        config={"workspace_ready": True},
    )
    await _create_repository(api_client, project["id"])
    task = await _create_task(api_client, project["id"], status=TaskStatus.TODO)

    # Wait for task_dispatcher to pick up the task
    max_attempts = 40
    for _attempt in range(max_attempts):
        resp = await api_client.get(f"/api/tasks/{task['id']}")
        if resp.status_code == 200 and resp.json()["status"] != TaskStatus.TODO:
            return  # Task was dispatched

        await asyncio.sleep(1)

    resp = await api_client.get(f"/api/tasks/{task['id']}")
    final_status = resp.json()["status"] if resp.status_code == 200 else "UNKNOWN"
    pytest.fail(f"Task not dispatched within {max_attempts} seconds. Final status: {final_status}")

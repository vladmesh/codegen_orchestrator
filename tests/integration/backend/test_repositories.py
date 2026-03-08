"""Integration tests for Repository CRUD — create, list, link to task."""

import os
from uuid import uuid4

import pytest

API_URL = os.getenv("API_URL", "http://api:8000")
TEST_TELEGRAM_ID = "999000"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repository_crud(api_client, seed_project):
    """Create a repository, read it, update it, delete it."""
    pid = f"repo-test-{uuid4().hex[:6]}"
    await seed_project(pid, name="Repo Test Project")

    # Create
    resp = await api_client.post(
        "/api/repositories/",
        json={
            "project_id": pid,
            "name": "test-repo",
            "git_url": "https://github.com/org/test-repo",
            "provider_repo_id": 12345,
        },
    )
    assert resp.status_code == 201
    repo = resp.json()
    repo_id = repo["id"]
    assert repo["name"] == "test-repo"
    assert repo["role"] == "primary"
    assert repo["is_managed"] is True
    assert repo["provider_repo_id"] == 12345

    # Read by ID
    resp = await api_client.get(f"/api/repositories/{repo_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == repo_id

    # Read by provider ID
    resp = await api_client.get("/api/repositories/by-provider-id/12345")
    assert resp.status_code == 200
    assert resp.json()["id"] == repo_id

    # List by project
    resp = await api_client.get(f"/api/repositories/?project_id={pid}")
    assert resp.status_code == 200
    repos = resp.json()
    assert any(r["id"] == repo_id for r in repos)

    # Update
    resp = await api_client.patch(
        f"/api/repositories/{repo_id}",
        json={"name": "updated-repo"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "updated-repo"

    # Delete
    resp = await api_client.delete(f"/api/repositories/{repo_id}")
    assert resp.status_code == 200

    # Verify deleted
    resp = await api_client.get(f"/api/repositories/{repo_id}")
    assert resp.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_task_with_repository_id(api_client, seed_project):
    """Create a task linked to a repository via repository_id FK."""
    pid = f"repo-task-{uuid4().hex[:6]}"
    await seed_project(pid, name="Repo Task Project")

    # Create repository
    resp = await api_client.post(
        "/api/repositories/",
        json={
            "project_id": pid,
            "name": "linked-repo",
            "git_url": "https://github.com/org/linked-repo",
        },
    )
    assert resp.status_code == 201
    repo_id = resp.json()["id"]

    # Create task with repository_id
    resp = await api_client.post(
        "/api/tasks/",
        json={
            "project_id": pid,
            "title": "Task in repo",
            "repository_id": repo_id,
        },
    )
    assert resp.status_code == 201
    task = resp.json()
    assert task["repository_id"] == repo_id

    # Filter tasks by repository_id
    resp = await api_client.get(f"/api/tasks/?repository_id={repo_id}")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) >= 1
    assert any(t["repository_id"] == repo_id for t in tasks)

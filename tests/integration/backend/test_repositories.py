"""Integration tests for Repository CRUD — create, list, link to task."""

import pytest


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repository_crud(api_client, seed_project):
    """Create a repository, read it, update it, delete it."""
    project = await seed_project(name="Repo Test Project")
    pid = project["id"]

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
async def test_task_with_repository_id(api_client, seed_project, seed_task):
    """Create a task linked to a repository via repository_id FK."""
    project = await seed_project(name="Repo Task Project")
    pid = project["id"]

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
    task = await seed_task(
        title="Task in repo",
        project_id=pid,
    )
    # Patch to add repository_id (TaskCreate may not accept it directly)
    resp = await api_client.patch(
        f"/api/tasks/{task['id']}",
        json={"repository_id": repo_id},
    )
    assert resp.status_code == 200
    assert resp.json()["repository_id"] == repo_id

    # Filter tasks by repository_id
    resp = await api_client.get(f"/api/tasks/?repository_id={repo_id}")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) >= 1
    assert any(t["repository_id"] == repo_id for t in tasks)

"""Step 5: Supervisor retry logic — Issue #4 (infinite retry loop).

Tests that the supervisor correctly handles different failure reasons.
Currently EXPECTED TO FAIL because:
1. Task model has no `failure_metadata` column — writes are silently ignored
2. Supervisor checks `failure_metadata.failure_reason` but it's always empty
3. No `ci_infra_failure` reason is recognized even if metadata existed

This test exposes the full chain: persist metadata → read it back → supervisor skips.
"""

import asyncio

import pytest

from shared.contracts.dto.task import TaskStatus


@pytest.mark.asyncio
async def test_failure_metadata_persists(api, test_project):
    """failure_metadata written via PATCH should be readable via GET.

    RED: TaskUpdate schema doesn't include failure_metadata, and the DB
    column doesn't exist. The PATCH silently drops it.
    """
    # Create a task
    resp = await api.post(
        "/api/tasks/",
        json={
            "project_id": test_project["id"],
            "title": "Infra failure metadata test",
        },
    )
    resp.raise_for_status()
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    # Transition: backlog → todo → in_dev → failed
    resp = await api.post(f"/api/tasks/{task_id}/transition?to_status={TaskStatus.TODO}")
    resp.raise_for_status()
    resp = await api.post(f"/api/tasks/{task_id}/start")
    resp.raise_for_status()
    resp = await api.post(f"/api/tasks/{task_id}/fail")
    resp.raise_for_status()

    # Try to set failure_metadata (this is what engineering consumer does)
    resp = await api.patch(
        f"/api/tasks/{task_id}",
        json={
            "failure_metadata": {
                "failure_reason": "ci_infra_failure",
                "error": "Registry auth failed",
            },
        },
    )
    resp.raise_for_status()
    # Should succeed (currently 200 but silently ignores the field)
    assert resp.status_code == 200

    # Read back — failure_metadata should be present
    resp = await api.get(f"/api/tasks/{task_id}")
    resp.raise_for_status()
    assert resp.status_code == 200
    task = resp.json()
    assert "failure_metadata" in task, "failure_metadata not in API response"
    assert task["failure_metadata"]["failure_reason"] == "ci_infra_failure"


@pytest.mark.asyncio
async def test_worker_rejected_metadata_persists(api, test_project):
    """worker_rejected failure_metadata should persist and be readable.

    RED: Same root cause as above — no DB column, no schema field.
    """
    resp = await api.post(
        "/api/tasks/",
        json={
            "project_id": test_project["id"],
            "title": "Worker rejected metadata test",
        },
    )
    resp.raise_for_status()
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    resp = await api.post(f"/api/tasks/{task_id}/transition?to_status={TaskStatus.TODO}")
    resp.raise_for_status()
    resp = await api.post(f"/api/tasks/{task_id}/start")
    resp.raise_for_status()
    resp = await api.post(f"/api/tasks/{task_id}/fail")
    resp.raise_for_status()

    resp = await api.patch(
        f"/api/tasks/{task_id}",
        json={
            "failure_metadata": {
                "failure_reason": "worker_rejected",
                "reject_reason": "REGISTRY_PASSWORD secret is empty",
            },
        },
    )
    resp.raise_for_status()
    assert resp.status_code == 200

    resp = await api.get(f"/api/tasks/{task_id}")
    resp.raise_for_status()
    task = resp.json()
    assert "failure_metadata" in task
    assert task["failure_metadata"]["failure_reason"] == "worker_rejected"


@pytest.mark.asyncio
async def test_infra_failed_task_not_retried_by_supervisor(api, test_project):
    """Task failed with ci_infra_failure should NOT be retried by supervisor.

    RED: Even if metadata persisted, supervisor only checks worker_rejected.
    This test creates a story + task, fails the task with infra metadata,
    then waits for one supervisor cycle and verifies the task stays failed.
    """
    # Create story (supervisor only handles story tasks)
    resp = await api.post(
        "/api/stories/",
        json={
            "project_id": test_project["id"],
            "title": "Infra failure story",
        },
    )
    resp.raise_for_status()
    assert resp.status_code == 201
    story_id = resp.json()["id"]

    # Create task linked to story
    resp = await api.post(
        "/api/tasks/",
        json={
            "project_id": test_project["id"],
            "title": "Infra failure task",
            "story_id": story_id,
        },
    )
    resp.raise_for_status()
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    # Transition to failed
    resp = await api.post(f"/api/tasks/{task_id}/transition?to_status={TaskStatus.TODO}")
    resp.raise_for_status()
    resp = await api.post(f"/api/tasks/{task_id}/start")
    resp.raise_for_status()
    resp = await api.post(f"/api/tasks/{task_id}/fail")
    resp.raise_for_status()

    # Set infra failure metadata
    resp = await api.patch(
        f"/api/tasks/{task_id}",
        json={
            "failure_metadata": {
                "failure_reason": "ci_infra_failure",
                "error": "Registry auth failed",
            },
        },
    )
    resp.raise_for_status()

    # Wait for supervisor cycle (runs every 30s)
    await asyncio.sleep(35)

    # Task should still be "failed" — supervisor should NOT have retried it
    resp = await api.get(f"/api/tasks/{task_id}")
    resp.raise_for_status()
    task = resp.json()
    assert task["status"] == TaskStatus.FAILED, (
        f"Expected task to stay 'failed' but got '{task['status']}'. "
        "Supervisor retried an infra-failed task (infinite loop bug)."
    )

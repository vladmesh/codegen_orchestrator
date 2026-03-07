"""Service test: WorkItem full lifecycle via real API + DB.

Tests the complete flow: create → start → events → complete → reopen.
Verifies state machine transitions, event history, and filters.
"""

from httpx import AsyncClient
import pytest

WI_TEST_PROJECT_ID = "test-work-items-proj"


@pytest.mark.asyncio
async def test_work_item_full_lifecycle(async_client: AsyncClient, _work_items_project):
    """create → start → iteration event → transition to testing → complete."""
    # 1. Create
    resp = await async_client.post(
        "/api/work-items/",
        json={
            "project_id": WI_TEST_PROJECT_ID,
            "title": "Lifecycle test feature",
            "type": "feature",
            "description": "Full lifecycle",
        },
    )
    assert resp.status_code == 201  # noqa: PLR2004
    wi = resp.json()
    wi_id = wi["id"]
    assert wi["status"] == "backlog"
    assert wi_id.startswith("wi-")

    # 2. Start (auto-promotes backlog → todo → in_dev)
    resp = await async_client.post(f"/api/work-items/{wi_id}/start", json={"actor": "po"})
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["status"] == "in_dev"

    # 3. Add iteration_start event
    resp = await async_client.post(
        f"/api/work-items/{wi_id}/events",
        json={
            "event_type": "iteration_start",
            "iteration": 0,
            "details": {"task_id": "eng-test-001"},
            "actor": "system",
        },
    )
    assert resp.status_code == 201  # noqa: PLR2004

    # 4. Transition to testing
    resp = await async_client.post(
        f"/api/work-items/{wi_id}/transition?to_status=testing",
        json={"actor": "system"},
    )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["status"] == "testing"

    # 5. Complete
    resp = await async_client.post(f"/api/work-items/{wi_id}/complete", json={"actor": "system"})
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["status"] == "done"

    # 6. Verify events
    resp = await async_client.get(f"/api/work-items/{wi_id}/events")
    assert resp.status_code == 200  # noqa: PLR2004
    events = resp.json()
    status_changes = [e for e in events if e["event_type"] == "status_change"]
    assert len(status_changes) >= 3  # noqa: PLR2004


@pytest.mark.asyncio
async def test_invalid_transition_rejected(async_client: AsyncClient, _work_items_project):
    """Cannot go from backlog directly to done."""
    resp = await async_client.post(
        "/api/work-items/",
        json={"project_id": WI_TEST_PROJECT_ID, "title": "Invalid test"},
    )
    wi_id = resp.json()["id"]

    resp = await async_client.post(f"/api/work-items/{wi_id}/complete")
    assert resp.status_code == 422  # noqa: PLR2004
    assert "Cannot transition" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_reopen_from_done(async_client: AsyncClient, _work_items_project):
    """done → backlog with reason, event recorded."""
    resp = await async_client.post(
        "/api/work-items/",
        json={"project_id": WI_TEST_PROJECT_ID, "title": "Reopen test"},
    )
    wi_id = resp.json()["id"]

    # Get to done
    await async_client.post(f"/api/work-items/{wi_id}/start")
    await async_client.post(f"/api/work-items/{wi_id}/transition?to_status=testing")
    await async_client.post(f"/api/work-items/{wi_id}/complete")

    # Reopen
    resp = await async_client.post(
        f"/api/work-items/{wi_id}/reopen",
        json={"reason": "Bug returned"},
    )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["status"] == "backlog"

    # Verify event
    resp = await async_client.get(f"/api/work-items/{wi_id}/events")
    events = resp.json()
    reopen = [
        e
        for e in events
        if e["event_type"] == "status_change"
        and e["from_status"] == "done"
        and e["to_status"] == "backlog"
    ]
    assert len(reopen) == 1
    assert reopen[0]["details"]["reason"] == "Bug returned"


@pytest.mark.asyncio
async def test_list_with_filters(async_client: AsyncClient, _work_items_project):
    """List returns items matching status filter."""
    resp = await async_client.post(
        "/api/work-items/",
        json={"project_id": WI_TEST_PROJECT_ID, "title": "Filter test"},
    )
    wi_id = resp.json()["id"]

    resp = await async_client.get("/api/work-items/?status=backlog")
    assert resp.status_code == 200  # noqa: PLR2004
    assert any(i["id"] == wi_id for i in resp.json())

    resp = await async_client.get("/api/work-items/?status=done")
    assert resp.status_code == 200  # noqa: PLR2004
    assert not any(i["id"] == wi_id for i in resp.json())


@pytest.mark.asyncio
async def test_comment_events_lifecycle(async_client: AsyncClient, _work_items_project):
    """create → start → comment events → complete, verify comment events in history."""
    # Create and start
    resp = await async_client.post(
        "/api/work-items/",
        json={"project_id": WI_TEST_PROJECT_ID, "title": "Comment events test", "type": "feature"},
    )
    assert resp.status_code == 201  # noqa: PLR2004
    wi_id = resp.json()["id"]
    await async_client.post(f"/api/work-items/{wi_id}/start", json={"actor": "claude"})

    # Comment 1
    resp = await async_client.post(
        f"/api/work-items/{wi_id}/events",
        json={
            "event_type": "comment",
            "details": {"text": "Starting implementation"},
            "actor": "engineer",
        },
    )
    assert resp.status_code == 201  # noqa: PLR2004
    assert resp.json()["event_type"] == "comment"

    # Note with commit info
    resp = await async_client.post(
        f"/api/work-items/{wi_id}/events",
        json={
            "event_type": "note",
            "details": {"action": "step_done", "step": 1, "commit_sha": "abc1234"},
            "actor": "claude",
        },
    )
    assert resp.status_code == 201  # noqa: PLR2004
    assert resp.json()["details"]["commit_sha"] == "abc1234"

    # Comment 2
    await async_client.post(
        f"/api/work-items/{wi_id}/events",
        json={
            "event_type": "comment",
            "details": {"text": "CI passed, deploying"},
            "actor": "ci",
        },
    )

    # Complete (need to go through testing first)
    await async_client.post(
        f"/api/work-items/{wi_id}/transition?to_status=testing",
        json={"actor": "claude"},
    )
    resp = await async_client.post(
        f"/api/work-items/{wi_id}/complete",
        json={"actor": "claude"},
    )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["status"] == "done"

    # Verify comment events
    resp = await async_client.get(f"/api/work-items/{wi_id}/events?event_type=comment")
    comments = resp.json()
    assert len(comments) == 2  # noqa: PLR2004
    assert comments[0]["details"]["text"] == "Starting implementation"
    assert comments[1]["details"]["text"] == "CI passed, deploying"


@pytest.mark.asyncio
async def test_update_metadata(async_client: AsyncClient, _work_items_project):
    """PATCH updates title/priority without changing status."""
    resp = await async_client.post(
        "/api/work-items/",
        json={"project_id": WI_TEST_PROJECT_ID, "title": "Patch test"},
    )
    wi_id = resp.json()["id"]

    resp = await async_client.patch(
        f"/api/work-items/{wi_id}",
        json={"title": "Updated title", "priority": 5},
    )
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert data["title"] == "Updated title"
    assert data["priority"] == 5  # noqa: PLR2004
    assert data["status"] == "backlog"

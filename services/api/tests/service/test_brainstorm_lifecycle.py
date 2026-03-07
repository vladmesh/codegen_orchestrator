"""Service test: Brainstorm full lifecycle via real API + DB.

Tests the complete flow: create → update → done → triage → archive.
Verifies state machine transitions and filters.
"""

from httpx import AsyncClient
import pytest

BS_TEST_PROJECT_ID = "test-work-items-proj"


@pytest.mark.asyncio
async def test_brainstorm_full_lifecycle(async_client: AsyncClient, _work_items_project):
    """create → update content → done → triage → archive."""
    # 1. Create
    resp = await async_client.post(
        "/api/brainstorms/",
        json={
            "project_id": BS_TEST_PROJECT_ID,
            "title": "Lifecycle test brainstorm",
            "created_by": "claude",
        },
    )
    assert resp.status_code == 201  # noqa: PLR2004
    bs = resp.json()
    bs_id = bs["id"]
    assert bs["status"] == "draft"
    assert bs_id.startswith("bs-")
    assert bs["created_by"] == "claude"

    # 2. Update content
    resp = await async_client.patch(
        f"/api/brainstorms/{bs_id}",
        json={"content": "# Analysis\n\n## Options\n\nA or B"},
    )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["content"] == "# Analysis\n\n## Options\n\nA or B"

    # 3. Mark done
    resp = await async_client.post(f"/api/brainstorms/{bs_id}/done", json={"actor": "claude"})
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["status"] == "done"

    # 4. Cannot mark done again
    resp = await async_client.post(f"/api/brainstorms/{bs_id}/done")
    assert resp.status_code == 409  # noqa: PLR2004

    # 5. Mark triaged
    resp = await async_client.post(f"/api/brainstorms/{bs_id}/triage", json={"actor": "triage"})
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["status"] == "triaged"

    # 6. Archive
    resp = await async_client.post(f"/api/brainstorms/{bs_id}/archive")
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["status"] == "archived"


@pytest.mark.asyncio
async def test_brainstorm_filters(async_client: AsyncClient, _work_items_project):
    """Test list endpoint with status and project_id filters."""
    # Create two brainstorms
    resp1 = await async_client.post(
        "/api/brainstorms/",
        json={"project_id": BS_TEST_PROJECT_ID, "title": "Filter test A"},
    )
    assert resp1.status_code == 201  # noqa: PLR2004
    bs_a_id = resp1.json()["id"]

    resp2 = await async_client.post(
        "/api/brainstorms/",
        json={"project_id": BS_TEST_PROJECT_ID, "title": "Filter test B"},
    )
    assert resp2.status_code == 201  # noqa: PLR2004

    # Mark A as done
    await async_client.post(f"/api/brainstorms/{bs_a_id}/done")

    # Filter by status=done
    resp = await async_client.get(f"/api/brainstorms/?status=done&project_id={BS_TEST_PROJECT_ID}")
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert any(bs["id"] == bs_a_id for bs in data)
    assert all(bs["status"] == "done" for bs in data)


@pytest.mark.asyncio
async def test_brainstorm_source_link(async_client: AsyncClient, _work_items_project):
    """Create work item with source_brainstorm_id linking back to brainstorm."""
    # Create brainstorm
    resp = await async_client.post(
        "/api/brainstorms/",
        json={"project_id": BS_TEST_PROJECT_ID, "title": "Source link test"},
    )
    assert resp.status_code == 201  # noqa: PLR2004
    bs_id = resp.json()["id"]

    # Create work item with source_brainstorm_id
    resp = await async_client.post(
        "/api/work-items/",
        json={
            "project_id": BS_TEST_PROJECT_ID,
            "title": "Task from brainstorm",
            "source_brainstorm_id": bs_id,
        },
    )
    assert resp.status_code == 201  # noqa: PLR2004
    wi = resp.json()
    assert wi["source_brainstorm_id"] == bs_id

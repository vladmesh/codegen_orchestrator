"""Service tests for new work item API endpoints: since, stats, next-tag, plan field."""

from httpx import AsyncClient
import pytest

WI_TEST_PROJECT_ID = "test-work-items-proj"


@pytest.mark.asyncio
async def test_stats_endpoint(async_client: AsyncClient, _work_items_project):
    """GET /api/work-items/stats returns status counts including total."""
    # Create a work item to ensure at least one exists
    await async_client.post(
        "/api/work-items/",
        json={"project_id": WI_TEST_PROJECT_ID, "title": "#900 Stats test"},
    )

    resp = await async_client.get("/api/work-items/stats")
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()

    assert "backlog" in data
    assert "todo" in data
    assert "in_dev" in data
    assert "done" in data
    assert "total" in data
    assert data["total"] >= 1
    assert data["backlog"] >= 1


@pytest.mark.asyncio
async def test_stats_with_project_filter(async_client: AsyncClient, _work_items_project):
    """Stats can be filtered by project_id."""
    resp = await async_client.get(f"/api/work-items/stats?project_id={WI_TEST_PROJECT_ID}")
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert "total" in data


@pytest.mark.asyncio
async def test_next_tag_endpoint(async_client: AsyncClient, _work_items_project):
    """GET /api/work-items/next-tag returns next available tag."""
    # Create items with known tags
    await async_client.post(
        "/api/work-items/",
        json={"project_id": WI_TEST_PROJECT_ID, "title": "#901 Tag test A"},
    )
    await async_client.post(
        "/api/work-items/",
        json={"project_id": WI_TEST_PROJECT_ID, "title": "#902 Tag test B"},
    )

    resp = await async_client.get("/api/work-items/next-tag")
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert "next_tag" in data
    assert data["next_tag"] >= 903  # noqa: PLR2004


@pytest.mark.asyncio
async def test_since_filter(async_client: AsyncClient, _work_items_project):
    """GET /api/work-items/?since=<date> filters by updated_at."""
    # Create a work item
    resp = await async_client.post(
        "/api/work-items/",
        json={"project_id": WI_TEST_PROJECT_ID, "title": "#903 Since test"},
    )
    assert resp.status_code == 201  # noqa: PLR2004

    # Filter with a past date — should include the item
    resp = await async_client.get("/api/work-items/?since=2020-01-01T00:00:00")
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert len(data) >= 1

    # Filter with a future date — should return empty
    resp = await async_client.get("/api/work-items/?since=2099-01-01T00:00:00")
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json() == []


@pytest.mark.asyncio
async def test_plan_field_patch(async_client: AsyncClient, _work_items_project):
    """PATCH /api/work-items/{id} can set and update plan field."""
    # Create
    resp = await async_client.post(
        "/api/work-items/",
        json={"project_id": WI_TEST_PROJECT_ID, "title": "#904 Plan test"},
    )
    wi_id = resp.json()["id"]
    assert resp.json()["plan"] is None

    # Set plan
    plan_text = "## Steps\n1. Do thing\n2. Test thing"
    resp = await async_client.patch(
        f"/api/work-items/{wi_id}",
        json={"plan": plan_text},
    )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["plan"] == plan_text

    # Verify via GET
    resp = await async_client.get(f"/api/work-items/{wi_id}")
    assert resp.json()["plan"] == plan_text

    # Update plan
    new_plan = "## Steps\n1. Do thing\n2. Test thing\n3. Deploy"
    resp = await async_client.patch(
        f"/api/work-items/{wi_id}",
        json={"plan": new_plan},
    )
    assert resp.json()["plan"] == new_plan


@pytest.mark.asyncio
async def test_plan_field_in_list(async_client: AsyncClient, _work_items_project):
    """Plan field is included in list responses."""
    resp = await async_client.post(
        "/api/work-items/",
        json={"project_id": WI_TEST_PROJECT_ID, "title": "#905 Plan list test"},
    )
    wi_id = resp.json()["id"]

    await async_client.patch(
        f"/api/work-items/{wi_id}",
        json={"plan": "Test plan"},
    )

    resp = await async_client.get(f"/api/work-items/?project_id={WI_TEST_PROJECT_ID}")
    items = resp.json()
    item = next(i for i in items if i["id"] == wi_id)
    assert item["plan"] == "Test plan"


@pytest.mark.asyncio
async def test_project_id_patch(async_client: AsyncClient, _work_items_project):
    """PATCH can update project_id."""
    resp = await async_client.post(
        "/api/work-items/",
        json={"project_id": WI_TEST_PROJECT_ID, "title": "#906 Project patch test"},
    )
    wi_id = resp.json()["id"]
    assert resp.json()["project_id"] == WI_TEST_PROJECT_ID

    resp = await async_client.patch(
        f"/api/work-items/{wi_id}",
        json={"project_id": WI_TEST_PROJECT_ID},
    )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["project_id"] == WI_TEST_PROJECT_ID


@pytest.mark.asyncio
async def test_comment_event_type(async_client: AsyncClient, _work_items_project):
    """Comment event type works correctly."""
    resp = await async_client.post(
        "/api/work-items/",
        json={"project_id": WI_TEST_PROJECT_ID, "title": "#907 Comment test"},
    )
    wi_id = resp.json()["id"]

    resp = await async_client.post(
        f"/api/work-items/{wi_id}/events",
        json={
            "event_type": "comment",
            "details": {"text": "This is a discussion comment"},
            "actor": "po",
        },
    )
    assert resp.status_code == 201  # noqa: PLR2004
    assert resp.json()["event_type"] == "comment"
    assert resp.json()["details"]["text"] == "This is a discussion comment"

    # Filter by comment type
    resp = await async_client.get(f"/api/work-items/{wi_id}/events?event_type=comment")
    comments = resp.json()
    assert len(comments) == 1
    assert comments[0]["actor"] == "po"


@pytest.mark.asyncio
async def test_step_event_types_rejected(async_client: AsyncClient, _work_items_project):
    """step_start and step_done event types are no longer valid."""
    resp = await async_client.post(
        "/api/work-items/",
        json={"project_id": WI_TEST_PROJECT_ID, "title": "#908 Step reject test"},
    )
    wi_id = resp.json()["id"]

    resp = await async_client.post(
        f"/api/work-items/{wi_id}/events",
        json={"event_type": "step_start", "details": {}, "actor": "claude"},
    )
    assert resp.status_code == 422  # noqa: PLR2004

    resp = await async_client.post(
        f"/api/work-items/{wi_id}/events",
        json={"event_type": "step_done", "details": {}, "actor": "claude"},
    )
    assert resp.status_code == 422  # noqa: PLR2004

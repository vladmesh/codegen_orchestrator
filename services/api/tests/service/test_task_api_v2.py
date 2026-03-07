"""Service tests for task API endpoints: since, stats, next-tag, plan field."""

from httpx import AsyncClient
import pytest

TASK_TEST_PROJECT_ID = "test-tasks-proj"


@pytest.mark.asyncio
async def test_stats_endpoint(async_client: AsyncClient, _tasks_project):
    """GET /api/tasks/stats returns status counts including total."""
    await async_client.post(
        "/api/tasks/",
        json={"project_id": TASK_TEST_PROJECT_ID, "title": "#900 Stats test"},
    )

    resp = await async_client.get("/api/tasks/stats")
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
async def test_stats_with_project_filter(async_client: AsyncClient, _tasks_project):
    """Stats can be filtered by project_id."""
    resp = await async_client.get(f"/api/tasks/stats?project_id={TASK_TEST_PROJECT_ID}")
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert "total" in data


@pytest.mark.asyncio
async def test_next_tag_endpoint(async_client: AsyncClient, _tasks_project):
    """GET /api/tasks/next-tag returns next available tag."""
    await async_client.post(
        "/api/tasks/",
        json={"project_id": TASK_TEST_PROJECT_ID, "title": "#901 Tag test A"},
    )
    await async_client.post(
        "/api/tasks/",
        json={"project_id": TASK_TEST_PROJECT_ID, "title": "#902 Tag test B"},
    )

    resp = await async_client.get("/api/tasks/next-tag")
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert "next_tag" in data
    assert data["next_tag"] >= 903  # noqa: PLR2004


@pytest.mark.asyncio
async def test_since_filter(async_client: AsyncClient, _tasks_project):
    """GET /api/tasks/?since=<date> filters by updated_at."""
    resp = await async_client.post(
        "/api/tasks/",
        json={"project_id": TASK_TEST_PROJECT_ID, "title": "#903 Since test"},
    )
    assert resp.status_code == 201  # noqa: PLR2004

    resp = await async_client.get("/api/tasks/?since=2020-01-01T00:00:00")
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert len(data) >= 1

    resp = await async_client.get("/api/tasks/?since=2099-01-01T00:00:00")
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json() == []


@pytest.mark.asyncio
async def test_plan_field_patch(async_client: AsyncClient, _tasks_project):
    """PATCH /api/tasks/{id} can set and update plan field."""
    resp = await async_client.post(
        "/api/tasks/",
        json={"project_id": TASK_TEST_PROJECT_ID, "title": "#904 Plan test"},
    )
    task_id = resp.json()["id"]
    assert resp.json()["plan"] is None

    plan_text = "## Steps\n1. Do thing\n2. Test thing"
    resp = await async_client.patch(
        f"/api/tasks/{task_id}",
        json={"plan": plan_text},
    )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["plan"] == plan_text

    resp = await async_client.get(f"/api/tasks/{task_id}")
    assert resp.json()["plan"] == plan_text

    new_plan = "## Steps\n1. Do thing\n2. Test thing\n3. Deploy"
    resp = await async_client.patch(
        f"/api/tasks/{task_id}",
        json={"plan": new_plan},
    )
    assert resp.json()["plan"] == new_plan


@pytest.mark.asyncio
async def test_plan_field_in_list(async_client: AsyncClient, _tasks_project):
    """Plan field is included in list responses."""
    resp = await async_client.post(
        "/api/tasks/",
        json={"project_id": TASK_TEST_PROJECT_ID, "title": "#905 Plan list test"},
    )
    task_id = resp.json()["id"]

    await async_client.patch(
        f"/api/tasks/{task_id}",
        json={"plan": "Test plan"},
    )

    resp = await async_client.get(f"/api/tasks/?project_id={TASK_TEST_PROJECT_ID}")
    items = resp.json()
    item = next(i for i in items if i["id"] == task_id)
    assert item["plan"] == "Test plan"


@pytest.mark.asyncio
async def test_project_id_patch(async_client: AsyncClient, _tasks_project):
    """PATCH can update project_id."""
    resp = await async_client.post(
        "/api/tasks/",
        json={"project_id": TASK_TEST_PROJECT_ID, "title": "#906 Project patch test"},
    )
    task_id = resp.json()["id"]
    assert resp.json()["project_id"] == TASK_TEST_PROJECT_ID

    resp = await async_client.patch(
        f"/api/tasks/{task_id}",
        json={"project_id": TASK_TEST_PROJECT_ID},
    )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["project_id"] == TASK_TEST_PROJECT_ID


@pytest.mark.asyncio
async def test_comment_event_type(async_client: AsyncClient, _tasks_project):
    """Comment event type works correctly."""
    resp = await async_client.post(
        "/api/tasks/",
        json={"project_id": TASK_TEST_PROJECT_ID, "title": "#907 Comment test"},
    )
    task_id = resp.json()["id"]

    resp = await async_client.post(
        f"/api/tasks/{task_id}/events",
        json={
            "event_type": "comment",
            "details": {"text": "This is a discussion comment"},
            "actor": "po",
        },
    )
    assert resp.status_code == 201  # noqa: PLR2004
    assert resp.json()["event_type"] == "comment"
    assert resp.json()["details"]["text"] == "This is a discussion comment"

    resp = await async_client.get(f"/api/tasks/{task_id}/events?event_type=comment")
    comments = resp.json()
    assert len(comments) == 1
    assert comments[0]["actor"] == "po"


@pytest.mark.asyncio
async def test_step_event_types_rejected(async_client: AsyncClient, _tasks_project):
    """step_start and step_done event types are no longer valid."""
    resp = await async_client.post(
        "/api/tasks/",
        json={"project_id": TASK_TEST_PROJECT_ID, "title": "#908 Step reject test"},
    )
    task_id = resp.json()["id"]

    resp = await async_client.post(
        f"/api/tasks/{task_id}/events",
        json={"event_type": "step_start", "details": {}, "actor": "claude"},
    )
    assert resp.status_code == 422  # noqa: PLR2004

    resp = await async_client.post(
        f"/api/tasks/{task_id}/events",
        json={"event_type": "step_done", "details": {}, "actor": "claude"},
    )
    assert resp.status_code == 422  # noqa: PLR2004
